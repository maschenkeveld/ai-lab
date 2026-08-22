// main.go
package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math/big"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strings"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"

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

Goal:
  - Export traces to grafana/otel-lgtm (OTLP/HTTP on :4318).
  - Export logs to grafana/otel-lgtm (OTLP/HTTP on :4318) with trace correlation.
  - Continue traces if upstream sends W3C trace context headers.
  - Create SERVER spans automatically for inbound HTTP requests (otelhttp middleware).
  - Correlate logs with traces via trace_id and span_id.
  - Provide DEBUG logs so we can verify whether trace headers are present and extracted.
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

	// =============================================================
	// LOGS → OTLP/HTTP → otel-lgtm:4318/v1/logs (Grafana Loki)
	//      + stdout (JSON) for docker compose logs
	// =============================================================
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
	otelHandler := otelslog.NewHandler("rest-book-flights", otelslog.WithLoggerProvider(logProvider))

	// Fanout to both handlers
	logger = slog.New(&fanoutHandler{handlers: []slog.Handler{stdoutHandler, otelHandler}})

	logger.Info("logging initialized",
		slog.String("protocol", "OTLP/HTTP + stdout"),
		slog.String("destination", logEndpoint+logPath),
		slog.String("trace_correlation", "automatic via otelslog bridge"),
	)

	return logProvider.Shutdown, nil
}

func getServiceResource() *resource.Resource {
	res, _ := resource.New(context.Background(),
		resource.WithAttributes(
			semconv.ServiceName(getenv("OTEL_SERVICE_NAME", "rest-book-flights")),
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

	// =============================================================
	// TRACES → OTLP/HTTP → otel-lgtm:4318/v1/traces (Grafana Tempo)
	// =============================================================
	//
	// NOTE: The OTEL Go SDK requires host:port separate from path,
	// so we can't use a single URL. The env var is for visibility only.
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
			semconv.ServiceName(getenv("OTEL_SERVICE_NAME", "rest-book-flights")),
			attribute.String("deployment.environment", getenv("OTEL_ENVIRONMENT", "ai-lab")),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("create resource: %w", err)
	}

	// AlwaysSample for dev/demo. For prod, consider ParentBased(TraceIDRatioBased(...))
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.AlwaysSample()),
		sdktrace.WithBatcher(exp),
	)

	otel.SetTracerProvider(tp)

	// Enable W3C propagation so we can join traces started by gateway or agent clients.
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

// ---------- Models ----------

type Booking struct {
	BookingCode  string `json:"booking_code"`
	FlightNumber string `json:"flight_number"` // KA-<FROM><TO>
	Status       string `json:"status"`        // scheduled
	Name         string `json:"name"`
	From         string `json:"from"`
	To           string `json:"to"`
	Date         string `json:"date"`
	CreatedAt    string `json:"created_at"`    // RFC3339
	CreatedAtTS  int64  `json:"created_at_ts"` // Unix seconds
}

type CreateBookingRequest struct {
	Name string `json:"name"`
	From string `json:"from"`
	To   string `json:"to"`
	Date string `json:"date"`
}

// ---------- Server ----------

type server struct {
	db *sql.DB
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
		slog.String("service", getenv("OTEL_SERVICE_NAME", "rest-book-flights")),
		slog.Bool("otel_logs_enabled", envBoolDefaultTrue("OTEL_LOGS_ENABLED")),
		slog.Bool("otel_traces_enabled", envBoolDefaultTrue("OTEL_TRACES_ENABLED")),
		slog.Bool("otel_propagation_enabled", envBoolDefaultTrue("OTEL_PROPAGATION_ENABLED")),
		slog.Bool("otel_metrics_enabled", envBoolDefaultTrue("OTEL_METRICS_ENABLED")),
	)

	dbPath := getenv("DB_PATH", "./bookings.db")

	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		logger.Error("failed to open database", slog.Any("error", err))
		os.Exit(1)
	}
	if err := initDB(db); err != nil {
		logger.Error("failed to initialize database", slog.Any("error", err))
		os.Exit(1)
	}

	logger.Info("database initialized", slog.String("path", dbPath))

	s := &server{db: db}

	mux := http.NewServeMux()
	mux.HandleFunc("/health", handleHealth)
	mux.HandleFunc("/v1/bookings", s.handleBookings)

	base := withCORS(withJSON(mux))

	// Debug wrapper (logs incoming propagation headers + extracted context)
	debugWrapper := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		debugIncomingPropagation(r)
		base.ServeHTTP(w, r)
	})

	// Wrap with otelhttp for automatic server spans
	otelHandler := otelhttp.NewHandler(debugWrapper, "rest.book-flights")

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
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()

	logger.Info("REST book-flights listening", slog.String("addr", addr))

	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Error("server listen error", slog.Any("error", err))
		os.Exit(1)
	}

	logger.Info("server shutdown complete")
}

// ---------- Handlers ----------

func handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":   true,
		"time": time.Now().UTC().Format(time.RFC3339),
	})
}

func (s *server) handleBookings(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	switch r.Method {
	case http.MethodPost:
		s.createBooking(w, r)
	case http.MethodGet:
		s.listBookings(w, r)
	default:
		logger.WarnContext(ctx, "method not allowed", slog.String("method", r.Method))
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET or POST")
	}
}

func (s *server) createBooking(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var req CreateBookingRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		logger.WarnContext(ctx, "invalid JSON body", slog.Any("error", err))
		writeError(w, http.StatusBadRequest, "bad_request", "invalid JSON body")
		return
	}

	b, err := validateBooking(req)
	if err != nil {
		logger.WarnContext(ctx, "booking validation failed",
			slog.String("name", req.Name),
			slog.Any("error", err),
		)
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}

	dbCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()

	const maxAttempts = 10
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		b.BookingCode, err = generatePNR()
		if err != nil {
			logger.ErrorContext(ctx, "failed to generate PNR", slog.Any("error", err))
			writeError(w, http.StatusInternalServerError, "internal_error", "failed to generate booking code")
			return
		}

		_, err = s.db.ExecContext(dbCtx, `
			INSERT INTO bookings (
				booking_code, flight_number, status, name, from_iata, to_iata, date, created_at, created_at_ts
			)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
		`, b.BookingCode, b.FlightNumber, b.Status, b.Name, b.From, b.To, b.Date, b.CreatedAt, b.CreatedAtTS)

		if err == nil {
			logger.InfoContext(ctx, "booking created",
				slog.String("booking_code", b.BookingCode),
				slog.String("flight_number", b.FlightNumber),
				slog.String("name", b.Name),
			)
			writeJSON(w, http.StatusOK, map[string]any{"booking": b})
			return
		}

		if isUniqueConstraintErr(err) {
			continue
		}
		logger.ErrorContext(ctx, "database error", slog.Any("error", err))
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}

	logger.ErrorContext(ctx, "could not generate unique booking code")
	writeError(w, http.StatusInternalServerError, "db_error", "could not generate a unique booking code")
}

func (s *server) listBookings(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	rows, err := s.db.Query(`
		SELECT booking_code, flight_number, status, name, from_iata, to_iata, date, created_at, created_at_ts
		FROM bookings
		ORDER BY created_at_ts ASC
	`)
	if err != nil {
		logger.ErrorContext(ctx, "database query failed", slog.Any("error", err))
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	defer rows.Close()

	var bookings []Booking
	for rows.Next() {
		var b Booking
		if err := rows.Scan(
			&b.BookingCode, &b.FlightNumber, &b.Status, &b.Name, &b.From, &b.To, &b.Date, &b.CreatedAt, &b.CreatedAtTS,
		); err != nil {
			logger.ErrorContext(ctx, "row scan failed", slog.Any("error", err))
			writeError(w, http.StatusInternalServerError, "db_error", err.Error())
			return
		}
		bookings = append(bookings, b)
	}

	logger.InfoContext(ctx, "bookings listed", slog.Int("count", len(bookings)))
	writeJSON(w, http.StatusOK, map[string]any{
		"bookings": bookings,
		"count":    len(bookings),
	})
}

// ---------- Validation + helpers ----------

var iataRe = regexp.MustCompile(`^[A-Z]{3}$`)
var flightNumberRe = regexp.MustCompile(`^KA-[A-Z]{6}$`)

const statusScheduled = "scheduled"

func validateBooking(req CreateBookingRequest) (Booking, error) {
	name := strings.TrimSpace(req.Name)
	from := strings.ToUpper(strings.TrimSpace(req.From))
	to := strings.ToUpper(strings.TrimSpace(req.To))
	date := strings.TrimSpace(req.Date)

	if name == "" {
		return Booking{}, errors.New("name is required")
	}
	if !iataRe.MatchString(from) || !iataRe.MatchString(to) {
		return Booking{}, errors.New("from/to must be 3-letter IATA codes")
	}
	if from == to {
		return Booking{}, errors.New("from and to must be different")
	}
	if _, err := time.Parse("2006-01-02", date); err != nil {
		return Booking{}, errors.New("date must be YYYY-MM-DD")
	}

	flightNumber := fmt.Sprintf("KA-%s%s", from, to)
	if !flightNumberRe.MatchString(flightNumber) {
		return Booking{}, errors.New("invalid flight_number generated")
	}

	now := time.Now().UTC()
	return Booking{
		BookingCode:  "", // generated on insert
		FlightNumber: flightNumber,
		Status:       statusScheduled,
		Name:         name,
		From:         from,
		To:           to,
		Date:         date,
		CreatedAt:    now.Format(time.RFC3339),
		CreatedAtTS:  now.Unix(),
	}, nil
}

// Airline-style PNR-like booking code:
// - 6 chars
// - uppercase letters + digits
// - excludes confusing characters: I, O, 0, 1
var pnrCharset = []rune("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")

func generatePNR() (string, error) {
	const n = 6
	out := make([]rune, n)
	max := big.NewInt(int64(len(pnrCharset)))

	for i := 0; i < n; i++ {
		r, err := rand.Int(rand.Reader, max)
		if err != nil {
			return "", err
		}
		out[i] = pnrCharset[r.Int64()]
	}
	return string(out), nil
}

func isUniqueConstraintErr(err error) bool {
	msg := strings.ToLower(err.Error())
	return strings.Contains(msg, "unique constraint failed") || strings.Contains(msg, "constraint failed")
}

// ---------- DB setup (fresh DB) ----------

func initDB(db *sql.DB) error {
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS bookings (
			booking_code   TEXT PRIMARY KEY,
			flight_number  TEXT NOT NULL,
			status         TEXT NOT NULL,
			name           TEXT NOT NULL,
			from_iata      TEXT NOT NULL,
			to_iata        TEXT NOT NULL,
			date           TEXT NOT NULL,
			created_at     TEXT NOT NULL,
			created_at_ts  INTEGER NOT NULL
		);
	`)
	return err
}

// ---------- Middleware / utils ----------

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
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
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
