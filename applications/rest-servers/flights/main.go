// main.go
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strings"
	"syscall"
	"time"

	// otelhttp instruments inbound HTTP requests
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	// otelslog bridges slog to OTEL
	"go.opentelemetry.io/contrib/bridges/otelslog"

	// Core OpenTelemetry APIs
	"go.opentelemetry.io/otel"

	// Attributes
	"go.opentelemetry.io/otel/attribute"

	// OTLP HTTP exporter - metrics
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"

	// OTLP HTTP exporter - logs
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"

	// OTLP HTTP exporter - traces
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"

	// Propagation
	"go.opentelemetry.io/otel/propagation"

	// SDK (log provider)
	sdklog "go.opentelemetry.io/otel/sdk/log"

	// SDK (meter provider)
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"

	// SDK (tracer provider)
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	// Semconv
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"

	// For debug header extraction
	"go.opentelemetry.io/otel/trace"
)

// Global logger
var logger *slog.Logger

/*
-------------------- OpenTelemetry bootstrap --------------------
*/

// fanoutHandler sends log records to multiple handlers (stdout + OTLP).
type fanoutHandler struct {
	handlers []slog.Handler
}

func (h *fanoutHandler) Enabled(ctx context.Context, level slog.Level) bool {
	for _, handler := range h.handlers {
		if handler.Enabled(ctx, level) {
			return true
		}
	}
	return false
}

func (h *fanoutHandler) Handle(ctx context.Context, r slog.Record) error {
	for _, handler := range h.handlers {
		if handler.Enabled(ctx, r.Level) {
			_ = handler.Handle(ctx, r.Clone())
		}
	}
	return nil
}

func (h *fanoutHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	handlers := make([]slog.Handler, len(h.handlers))
	for i, handler := range h.handlers {
		handlers[i] = handler.WithAttrs(attrs)
	}
	return &fanoutHandler{handlers: handlers}
}

func (h *fanoutHandler) WithGroup(name string) slog.Handler {
	handlers := make([]slog.Handler, len(h.handlers))
	for i, handler := range h.handlers {
		handlers[i] = handler.WithGroup(name)
	}
	return &fanoutHandler{handlers: handlers}
}

func initLogging(ctx context.Context) (shutdown func(context.Context) error, err error) {
	stdoutHandler := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelDebug})

	if !envBoolDefaultTrue("OTEL_LOGS_ENABLED") {
		logger = slog.New(stdoutHandler)
		logger.Info("OpenTelemetry logging disabled")
		return func(context.Context) error { return nil }, nil
	}

	logEndpoint := getenvWithFallback("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT", "otel-lgtm:4318")
	logPath := "/v1/logs"

	logExp, err := otlploghttp.New(ctx,
		otlploghttp.WithEndpoint(logEndpoint),
		otlploghttp.WithURLPath(logPath),
		otlploghttp.WithInsecure(),
	)
	if err != nil {
		return nil, fmt.Errorf("create OTLP log exporter: %w", err)
	}

	logProvider := sdklog.NewLoggerProvider(
		sdklog.WithResource(getServiceResource()),
		sdklog.WithProcessor(sdklog.NewBatchProcessor(logExp)),
	)

	// Create handlers: stdout (JSON) + OTLP (with trace correlation)
	otelHandler := otelslog.NewHandler("rest-flights", otelslog.WithLoggerProvider(logProvider))

	// Fanout to both handlers
	logger = slog.New(&fanoutHandler{handlers: []slog.Handler{stdoutHandler, otelHandler}})

	logger.Info("logging initialized",
		slog.String("protocol", "OTLP/HTTP + stdout"),
		slog.String("destination", logEndpoint+logPath),
	)

	return logProvider.Shutdown, nil
}

func getServiceResource() *resource.Resource {
	res, _ := resource.New(context.Background(),
		resource.WithAttributes(
			semconv.ServiceName(getenv("OTEL_SERVICE_NAME", "rest-flights")),
			attribute.String("deployment.environment", getenv("OTEL_ENVIRONMENT", "ai-lab")),
		),
	)
	return res
}

func initTracer(ctx context.Context) (shutdown func(context.Context) error, err error) {
	if !envBoolDefaultTrue("OTEL_TRACES_ENABLED") {
		if envBoolDefaultTrue("OTEL_PROPAGATION_ENABLED") {
			otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
				propagation.TraceContext{},
				propagation.Baggage{},
			))
		}
		logger.Info("OpenTelemetry tracing disabled",
			slog.Bool("otel_propagation_enabled", envBoolDefaultTrue("OTEL_PROPAGATION_ENABLED")),
		)
		return func(context.Context) error { return nil }, nil
	}

	traceEndpoint := getenvWithFallback("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT", "otel-lgtm:4318")
	tracePath := "/v1/traces"

	exp, err := otlptracehttp.New(ctx,
		otlptracehttp.WithEndpoint(traceEndpoint),
		otlptracehttp.WithURLPath(tracePath),
		otlptracehttp.WithInsecure(),
	)
	if err != nil {
		return nil, fmt.Errorf("create OTLP trace exporter: %w", err)
	}

	logger.Info("traces exporter configured",
		slog.String("protocol", "OTLP/HTTP"),
		slog.String("destination", traceEndpoint+tracePath),
	)

	res, err := resource.New(ctx,
		resource.WithFromEnv(),
		resource.WithAttributes(
			semconv.ServiceName(getenv("OTEL_SERVICE_NAME", "rest-flights")),
			attribute.String("deployment.environment", getenv("OTEL_ENVIRONMENT", "ai-lab")),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("create resource: %w", err)
	}

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.AlwaysSample()),
		sdktrace.WithBatcher(exp),
	)

	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	return tp.Shutdown, nil
}

func initMetrics(ctx context.Context) (shutdown func(context.Context) error, err error) {
	if !envBoolDefaultTrue("OTEL_METRICS_ENABLED") {
		logger.Info("OpenTelemetry metrics disabled")
		return func(context.Context) error { return nil }, nil
	}

	metricEndpoint := getenvWithFallback("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT", "otel-lgtm:4318")
	metricPath := "/v1/metrics"

	exp, err := otlpmetrichttp.New(ctx,
		otlpmetrichttp.WithEndpoint(metricEndpoint),
		otlpmetrichttp.WithURLPath(metricPath),
		otlpmetrichttp.WithInsecure(),
	)
	if err != nil {
		return nil, fmt.Errorf("create OTLP metric exporter: %w", err)
	}

	mp := sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(getServiceResource()),
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exp)),
	)

	otel.SetMeterProvider(mp)
	logger.Info("metrics exporter configured",
		slog.String("protocol", "OTLP/HTTP"),
		slog.String("destination", metricEndpoint+metricPath),
	)

	return mp.Shutdown, nil
}

func debugIncomingPropagation(r *http.Request) {
	tp := r.Header.Get("traceparent")
	ts := r.Header.Get("tracestate")
	bg := r.Header.Get("baggage")

	ctx := otel.GetTextMapPropagator().Extract(r.Context(), propagation.HeaderCarrier(r.Header))
	sc := trace.SpanContextFromContext(ctx)

	logger.InfoContext(ctx, "incoming propagation headers",
		slog.String("method", r.Method),
		slog.String("path", r.URL.Path),

		slog.String("traceparent", tp),
		slog.String("tracestate", ts),
		slog.String("baggage", bg),

		slog.Bool("extracted_valid", sc.IsValid()),
		slog.Bool("extracted_remote", sc.IsRemote()),
		slog.String("extracted_trace_id", sc.TraceID().String()),
		slog.String("extracted_span_id", sc.SpanID().String()),
	)
}

type Airport struct {
	Code string  `json:"code"`
	Name string  `json:"name"`
	City string  `json:"city"`
	Ctry string  `json:"country"`
	Lat  float64 `json:"lat"`
	Lon  float64 `json:"lon"`
}

type PriceResponse struct {
	From         string  `json:"from"`
	To           string  `json:"to"`
	Date         string  `json:"date"`
	Month        int     `json:"month"`
	DistanceKm   float64 `json:"distance_km"`
	Currency     string  `json:"currency"`
	FictivePrice float64 `json:"price"`
	FlightNumber string  `json:"flight_number"`

	Breakdown struct {
		BasePerKm         float64 `json:"base_per_km"`
		BaseFare          float64 `json:"base_fare"`
		DistanceComponent float64 `json:"distance_component"`
		MonthMultiplier   float64 `json:"month_multiplier"`
		Jitter            float64 `json:"jitter"`
	} `json:"breakdown"`
}

type PriceRequest struct {
	From string `json:"from"`
	To   string `json:"to"`
	Date string `json:"date"` // YYYY-MM-DD
}

var airports = map[string]Airport{
	"AMS": {Code: "AMS", Name: "Amsterdam Airport Schiphol", City: "Amsterdam", Ctry: "NL", Lat: 52.3105, Lon: 4.7683},
	"EIN": {Code: "EIN", Name: "Eindhoven Airport", City: "Eindhoven", Ctry: "NL", Lat: 51.4501, Lon: 5.3745},
	"BRU": {Code: "BRU", Name: "Brussels Airport", City: "Brussels", Ctry: "BE", Lat: 50.9010, Lon: 4.4844},
	"CDG": {Code: "CDG", Name: "Charles de Gaulle", City: "Paris", Ctry: "FR", Lat: 49.0097, Lon: 2.5479},
	"ORY": {Code: "ORY", Name: "Paris Orly", City: "Paris", Ctry: "FR", Lat: 48.7262, Lon: 2.3652},
	"LHR": {Code: "LHR", Name: "Heathrow", City: "London", Ctry: "GB", Lat: 51.4700, Lon: -0.4543},
	"LGW": {Code: "LGW", Name: "Gatwick", City: "London", Ctry: "GB", Lat: 51.1537, Lon: -0.1821},
	"STN": {Code: "STN", Name: "London Stansted", City: "London", Ctry: "GB", Lat: 51.8850, Lon: 0.2350},
	"LTN": {Code: "LTN", Name: "London Luton", City: "London", Ctry: "GB", Lat: 51.8755, Lon: -0.3729},
	"LCY": {Code: "LCY", Name: "London City", City: "London", Ctry: "GB", Lat: 51.5050, Lon: 0.0550},
	"SEN": {Code: "SEN", Name: "London Southend", City: "London", Ctry: "GB", Lat: 51.5698, Lon: 0.7037},
	"DUB": {Code: "DUB", Name: "Dublin Airport", City: "Dublin", Ctry: "IE", Lat: 53.4213, Lon: -6.2701},
	"BCN": {Code: "BCN", Name: "Barcelona–El Prat", City: "Barcelona", Ctry: "ES", Lat: 41.2974, Lon: 2.0833},
	"MAD": {Code: "MAD", Name: "Adolfo Suárez Madrid–Barajas", City: "Madrid", Ctry: "ES", Lat: 40.4983, Lon: -3.5676},
	"LIS": {Code: "LIS", Name: "Humberto Delgado", City: "Lisbon", Ctry: "PT", Lat: 38.7742, Lon: -9.1342},
	"FCO": {Code: "FCO", Name: "Rome Fiumicino", City: "Rome", Ctry: "IT", Lat: 41.7999, Lon: 12.2462},
	"CIA": {Code: "CIA", Name: "Rome Ciampino (G. B. Pastine)", City: "Rome", Ctry: "IT", Lat: 41.7994, Lon: 12.5972},
	"MXP": {Code: "MXP", Name: "Milan Malpensa", City: "Milan", Ctry: "IT", Lat: 45.6301, Lon: 8.7231},
	"LIN": {Code: "LIN", Name: "Milan Linate", City: "Milan", Ctry: "IT", Lat: 45.4451, Lon: 9.2767},
	"BGY": {Code: "BGY", Name: "Milan Bergamo (Orio al Serio)", City: "Bergamo", Ctry: "IT", Lat: 45.6689, Lon: 9.7003},
	"BER": {Code: "BER", Name: "Berlin Brandenburg", City: "Berlin", Ctry: "DE", Lat: 52.3667, Lon: 13.5033},
	"FRA": {Code: "FRA", Name: "Frankfurt Airport", City: "Frankfurt", Ctry: "DE", Lat: 50.0379, Lon: 8.5622},
	"MUC": {Code: "MUC", Name: "Munich Airport", City: "Munich", Ctry: "DE", Lat: 48.3538, Lon: 11.7861},
	"CPH": {Code: "CPH", Name: "Copenhagen Airport", City: "Copenhagen", Ctry: "DK", Lat: 55.6180, Lon: 12.6508},
	"ARN": {Code: "ARN", Name: "Stockholm Arlanda", City: "Stockholm", Ctry: "SE", Lat: 59.6519, Lon: 17.9186},
	"OSL": {Code: "OSL", Name: "Oslo Gardermoen", City: "Oslo", Ctry: "NO", Lat: 60.1939, Lon: 11.1004},
	"HEL": {Code: "HEL", Name: "Helsinki Airport", City: "Helsinki", Ctry: "FI", Lat: 60.3172, Lon: 24.9633},
	"VIE": {Code: "VIE", Name: "Vienna International", City: "Vienna", Ctry: "AT", Lat: 48.1103, Lon: 16.5697},
	"ZRH": {Code: "ZRH", Name: "Zürich Airport", City: "Zürich", Ctry: "CH", Lat: 47.4581, Lon: 8.5555},
	"GVA": {Code: "GVA", Name: "Geneva Airport", City: "Geneva", Ctry: "CH", Lat: 46.2381, Lon: 6.1089},
	"PRG": {Code: "PRG", Name: "Václav Havel Airport Prague", City: "Prague", Ctry: "CZ", Lat: 50.1008, Lon: 14.2632},
	"WAW": {Code: "WAW", Name: "Warsaw Chopin", City: "Warsaw", Ctry: "PL", Lat: 52.1657, Lon: 20.9671},
	"ATH": {Code: "ATH", Name: "Athens International", City: "Athens", Ctry: "GR", Lat: 37.9364, Lon: 23.9445},
}

func main() {
	ctx := context.Background()

	// Initialize logging first
	logShutdown, err := initLogging(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "otel logging init failed: %v\n", err)
		os.Exit(1)
	}
	defer func() { _ = logShutdown(context.Background()) }()

	// Initialize tracing
	traceShutdown, err := initTracer(ctx)
	if err != nil {
		logger.Error("otel tracing init failed", slog.Any("error", err))
		os.Exit(1)
	}
	defer func() { _ = traceShutdown(context.Background()) }()

	metricShutdown, err := initMetrics(ctx)
	if err != nil {
		logger.Error("otel metrics init failed", slog.Any("error", err))
		os.Exit(1)
	}
	defer func() { _ = metricShutdown(context.Background()) }()

	logger.Info("OpenTelemetry initialized (logs + traces + metrics)")
	logger.Info("service startup completed",
		slog.String("service", getenv("OTEL_SERVICE_NAME", "rest-flights")),
		slog.Bool("otel_logs_enabled", envBoolDefaultTrue("OTEL_LOGS_ENABLED")),
		slog.Bool("otel_traces_enabled", envBoolDefaultTrue("OTEL_TRACES_ENABLED")),
		slog.Bool("otel_propagation_enabled", envBoolDefaultTrue("OTEL_PROPAGATION_ENABLED")),
		slog.Bool("otel_metrics_enabled", envBoolDefaultTrue("OTEL_METRICS_ENABLED")),
	)

	mux := http.NewServeMux()
	mux.HandleFunc("/health", handleHealth)
	mux.HandleFunc("/v1/airports", handleAirports)
	mux.HandleFunc("/v1/price", handlePrice)

	base := withCORS(withJSON(mux))

	// Debug wrapper (logs incoming propagation headers + extracted context)
	debugWrapper := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		debugIncomingPropagation(r)
		base.ServeHTTP(w, r)
	})

	// Wrap with otelhttp for automatic server spans
	otelHandler := otelhttp.NewHandler(debugWrapper, "rest.flights")

	addr := getenv("ADDR", ":8000")

	srv := &http.Server{
		Addr:              addr,
		Handler:           otelHandler,
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		ch := make(chan os.Signal, 1)
		signal.Notify(ch, syscall.SIGINT, syscall.SIGTERM)
		<-ch
		logger.Info("shutting down HTTP server")
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	}()

	logger.Info("REST flights listening", slog.String("addr", addr))

	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Error("server listen error", slog.Any("error", err))
		os.Exit(1)
	}

	logger.Info("server shutdown complete")
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "time": time.Now().UTC().Format(time.RFC3339)})
}

func handleAirports(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	if r.Method != http.MethodGet {
		logger.WarnContext(ctx, "method not allowed", slog.String("method", r.Method))
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}
	list := make([]Airport, 0, len(airports))
	for _, a := range airports {
		list = append(list, a)
	}
	sort.Slice(list, func(i, j int) bool { return list[i].Code < list[j].Code })

	logger.InfoContext(ctx, "airports listed", slog.Int("count", len(list)))
	writeJSON(w, http.StatusOK, map[string]any{"airports": list, "count": len(list)})
}

func handlePrice(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	switch r.Method {
	case http.MethodGet:
		req := PriceRequest{
			From: r.URL.Query().Get("from"),
			To:   r.URL.Query().Get("to"),
			Date: r.URL.Query().Get("date"),
		}
		resp, err := computePrice(req)
		if err != nil {
			logger.WarnContext(ctx, "price computation failed",
				slog.String("from", req.From),
				slog.String("to", req.To),
				slog.Any("error", err),
			)
			writeError(w, http.StatusBadRequest, "bad_request", err.Error())
			return
		}
		logger.InfoContext(ctx, "price computed",
			slog.String("from", resp.From),
			slog.String("to", resp.To),
			slog.Float64("price", resp.FictivePrice),
		)
		writeJSON(w, http.StatusOK, resp)
	case http.MethodPost:
		var req PriceRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			logger.WarnContext(ctx, "invalid JSON body", slog.Any("error", err))
			writeError(w, http.StatusBadRequest, "bad_request", "invalid JSON body")
			return
		}
		resp, err := computePrice(req)
		if err != nil {
			logger.WarnContext(ctx, "price computation failed",
				slog.String("from", req.From),
				slog.String("to", req.To),
				slog.Any("error", err),
			)
			writeError(w, http.StatusBadRequest, "bad_request", err.Error())
			return
		}
		logger.InfoContext(ctx, "price computed",
			slog.String("from", resp.From),
			slog.String("to", resp.To),
			slog.Float64("price", resp.FictivePrice),
		)
		writeJSON(w, http.StatusOK, resp)
	default:
		logger.WarnContext(ctx, "method not allowed", slog.String("method", r.Method))
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET or POST")
	}
}

func computePrice(req PriceRequest) (*PriceResponse, error) {
	from := strings.ToUpper(strings.TrimSpace(req.From))
	to := strings.ToUpper(strings.TrimSpace(req.To))
	if len(from) != 3 || len(to) != 3 {
		return nil, errors.New("from/to must be 3-letter IATA codes")
	}
	if from == to {
		return nil, errors.New("from and to must be different")
	}

	aFrom, ok := airports[from]
	if !ok {
		return nil, fmt.Errorf("unknown origin airport: %s", from)
	}
	aTo, ok := airports[to]
	if !ok {
		return nil, fmt.Errorf("unknown destination airport: %s", to)
	}

	d, err := parseDate(req.Date)
	if err != nil {
		return nil, errors.New("date must be YYYY-MM-DD")
	}

	distKm := haversineKm(aFrom.Lat, aFrom.Lon, aTo.Lat, aTo.Lon)

	// Pricing model (fictive):
	// base fare + distance component * month multiplier + small deterministic jitter.
	baseFare := 18.0
	basePerKm := 0.09 // 9 cents/km
	distanceComponent := distKm * basePerKm

	monthMult := monthMultiplier(d.Month())
	jitter := deterministicJitterEUR(from, to, d) // -12..+12 EUR-ish

	raw := (baseFare + distanceComponent) * monthMult
	price := round2(max(19.0, raw+jitter))

	flightNumber := fmt.Sprintf("KA-%s%s", from, to)

	resp := &PriceResponse{
		From:         from,
		To:           to,
		Date:         d.Format("2006-01-02"),
		Month:        int(d.Month()),
		DistanceKm:   round2(distKm),
		Currency:     "EUR",
		FictivePrice: price,
		FlightNumber: flightNumber,
	}
	resp.Breakdown.BasePerKm = basePerKm
	resp.Breakdown.BaseFare = baseFare
	resp.Breakdown.DistanceComponent = round2(distanceComponent)
	resp.Breakdown.MonthMultiplier = monthMult
	resp.Breakdown.Jitter = round2(jitter)
	return resp, nil
}

func parseDate(s string) (time.Time, error) {
	s = strings.TrimSpace(s)
	return time.Parse("2006-01-02", s)
}

func monthMultiplier(m time.Month) float64 {
	// Simple seasonality:
	// - Summer peaks (Jun-Aug)
	// - Holiday bump (Dec)
	// - Shoulder seasons cheaper (Jan-Feb, Nov)
	switch m {
	case time.January:
		return 0.88
	case time.February:
		return 0.92
	case time.March:
		return 0.98
	case time.April:
		return 1.03
	case time.May:
		return 1.08
	case time.June:
		return 1.20
	case time.July:
		return 1.28
	case time.August:
		return 1.25
	case time.September:
		return 1.10
	case time.October:
		return 1.02
	case time.November:
		return 0.90
	case time.December:
		return 1.22
	default:
		return 1.0
	}
}

func deterministicJitterEUR(from, to string, d time.Time) float64 {
	// Deterministic pseudo-random jitter based on route+date (stable for same inputs).
	seed := fmt.Sprintf("%s-%s-%s", from, to, d.Format("2006-01-02"))
	sum := sha256.Sum256([]byte(seed))
	hexStr := hex.EncodeToString(sum[:])

	// Take first 8 hex chars => 32 bits.
	var v uint32
	_, _ = fmt.Sscanf(hexStr[:8], "%x", &v)

	// Map to [-12, +12]
	x := float64(v%2400) / 100.0 // 0..24.00
	return x - 12.0
}

func haversineKm(lat1, lon1, lat2, lon2 float64) float64 {
	const R = 6371.0 // km
	φ1 := lat1 * math.Pi / 180
	φ2 := lat2 * math.Pi / 180
	Δφ := (lat2 - lat1) * math.Pi / 180
	Δλ := (lon2 - lon1) * math.Pi / 180

	a := math.Sin(Δφ/2)*math.Sin(Δφ/2) +
		math.Cos(φ1)*math.Cos(φ2)*math.Sin(Δλ/2)*math.Sin(Δλ/2)
	c := 2 * math.Atan2(math.Sqrt(a), math.Sqrt(1-a))
	return R * c
}

func round2(x float64) float64 { return math.Round(x*100) / 100 }
func max(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}

func getenv(k, def string) string {
	if v := os.Getenv(k); strings.TrimSpace(v) != "" {
		return v
	}
	return def
}

func envBoolDefaultTrue(k string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(k))) {
	case "0", "false", "no", "off":
		return false
	default:
		return true
	}
}

// getenvWithFallback checks primary env var, then fallback, then default.
// Follows OTEL convention: signal-specific > base endpoint > default.
// For OTLP endpoints, strips http:// or https:// since WithEndpoint() expects host:port.
func getenvWithFallback(primary, fallback, def string) string {
	if v := os.Getenv(primary); strings.TrimSpace(v) != "" {
		return stripURLScheme(v)
	}
	if v := os.Getenv(fallback); strings.TrimSpace(v) != "" {
		return stripURLScheme(v)
	}
	return def
}

// stripURLScheme removes http:// or https:// prefix for WithEndpoint().
func stripURLScheme(s string) string {
	s = strings.TrimPrefix(s, "http://")
	s = strings.TrimPrefix(s, "https://")
	return s
}

// --- middleware + helpers ---

func withJSON(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		next.ServeHTTP(w, r)
	})
}

func withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]any{
		"error": map[string]any{
			"code":    code,
			"message": message,
		},
	})
}
