package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"go.opentelemetry.io/contrib/bridges/otelslog"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
)

var logger *slog.Logger
var cachedDestinations DestinationsDB

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

	otelHandler := otelslog.NewHandler("rest-destinations", otelslog.WithLoggerProvider(logProvider))
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
			semconv.ServiceName(getenv("OTEL_SERVICE_NAME", "rest-destinations")),
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

	res, err := resource.New(ctx,
		resource.WithFromEnv(),
		resource.WithAttributes(
			semconv.ServiceName(getenv("OTEL_SERVICE_NAME", "rest-destinations")),
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

	logger.Info("traces exporter configured",
		slog.String("protocol", "OTLP/HTTP"),
		slog.String("destination", traceEndpoint+tracePath),
	)

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
	ctx := otel.GetTextMapPropagator().Extract(r.Context(), propagation.HeaderCarrier(r.Header))
	sc := trace.SpanContextFromContext(ctx)

	logger.InfoContext(ctx, "incoming propagation headers",
		slog.String("method", r.Method),
		slog.String("path", r.URL.Path),
		slog.String("traceparent", r.Header.Get("traceparent")),
		slog.Bool("extracted_valid", sc.IsValid()),
		slog.Bool("extracted_remote", sc.IsRemote()),
		slog.String("extracted_trace_id", sc.TraceID().String()),
		slog.String("extracted_span_id", sc.SpanID().String()),
	)
}

type Destination struct {
	ID               string   `json:"id"`
	Name             string   `json:"name"`
	Country          string   `json:"country"`
	Region           string   `json:"region"`
	Vibes            []string `json:"vibes"`
	BudgetLevel      string   `json:"budget_level"`
	AirportIATACodes []string `json:"airport_iata_codes"`
	Activities       []string `json:"activities"`
	Blurb            string   `json:"blurb"`
}

type DestinationsDB struct {
	Destinations []Destination `json:"destinations"`
}

type ListDestinationsRequest struct {
	Vibes           []string `json:"vibes,omitempty"`
	BudgetLevel     string   `json:"budget_level,omitempty"`
	Activities      []string `json:"activities,omitempty"`
	RequireAllVibes bool     `json:"require_all_vibes,omitempty"`
	Limit           int      `json:"limit,omitempty"`
}

type ShortlistDestinationsRequest struct {
	Query string `json:"query"`
	Limit int    `json:"limit,omitempty"`
}

var allowedVibes = map[string]struct{}{
	"city": {}, "beach": {}, "nature": {}, "culture": {},
	"food": {}, "nightlife": {}, "relax": {}, "design": {},
}

var allowedBudget = map[string]struct{}{
	"low": {}, "mid": {}, "high": {},
}

func main() {
	ctx := context.Background()

	logShutdown, err := initLogging(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "otel logging init failed: %v\n", err)
		os.Exit(1)
	}
	defer func() { _ = logShutdown(context.Background()) }()

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
		slog.String("service", getenv("OTEL_SERVICE_NAME", "rest-destinations")),
		slog.Bool("otel_logs_enabled", envBoolDefaultTrue("OTEL_LOGS_ENABLED")),
		slog.Bool("otel_traces_enabled", envBoolDefaultTrue("OTEL_TRACES_ENABLED")),
		slog.Bool("otel_propagation_enabled", envBoolDefaultTrue("OTEL_PROPAGATION_ENABLED")),
		slog.Bool("otel_metrics_enabled", envBoolDefaultTrue("OTEL_METRICS_ENABLED")),
	)

	cachedDestinations, err = readDestinations()
	if err != nil {
		logger.Error("failed to load destinations", slog.Any("error", err))
		os.Exit(1)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/health", handleHealth)
	mux.HandleFunc("/v1/destinations", handleDestinations)
	mux.HandleFunc("/v1/destinations/", handleDestinationByID)
	mux.HandleFunc("/v1/shortlist", handleShortlist)

	base := withCORS(withJSON(mux))
	debugWrapper := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		debugIncomingPropagation(r)
		base.ServeHTTP(w, r)
	})

	addr := getenv("ADDR", ":8000")
	srv := &http.Server{
		Addr:              addr,
		Handler:           otelhttp.NewHandler(debugWrapper, "rest.destinations"),
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

	logger.Info("REST destinations listening", slog.String("addr", addr))

	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Error("server listen error", slog.Any("error", err))
		os.Exit(1)
	}
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":      true,
		"service": "rest-destinations",
		"time":    time.Now().UTC().Format(time.RFC3339),
	})
}

func handleDestinations(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		req := ListDestinationsRequest{
			Vibes:           splitCSV(r.URL.Query().Get("vibes")),
			BudgetLevel:     r.URL.Query().Get("budget_level"),
			Activities:      splitCSV(r.URL.Query().Get("activities")),
			RequireAllVibes: parseBool(r.URL.Query().Get("require_all_vibes")),
			Limit:           parseInt(r.URL.Query().Get("limit")),
		}
		writeListResponse(w, r, req)
	case http.MethodPost:
		var req ListDestinationsRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeError(w, http.StatusBadRequest, "bad_request", "invalid JSON body")
			return
		}
		writeListResponse(w, r, req)
	default:
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET or POST")
	}
}

func handleDestinationByID(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}

	id := strings.TrimPrefix(r.URL.Path, "/v1/destinations/")
	id = strings.TrimSpace(id)
	if id == "" {
		writeError(w, http.StatusBadRequest, "bad_request", "id is required")
		return
	}

	d, ok := findDestination(id)
	if !ok {
		writeError(w, http.StatusNotFound, "not_found", fmt.Sprintf("destination %q not found", id))
		return
	}

	logger.InfoContext(r.Context(), "destination retrieved", slog.String("id", d.ID), slog.String("name", d.Name))
	writeJSON(w, http.StatusOK, d)
}

func handleShortlist(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		req := ShortlistDestinationsRequest{
			Query: r.URL.Query().Get("query"),
			Limit: parseInt(r.URL.Query().Get("limit")),
		}
		writeShortlistResponse(w, r, req)
	case http.MethodPost:
		var req ShortlistDestinationsRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeError(w, http.StatusBadRequest, "bad_request", "invalid JSON body")
			return
		}
		writeShortlistResponse(w, r, req)
	default:
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use GET or POST")
	}
}

func writeListResponse(w http.ResponseWriter, r *http.Request, req ListDestinationsRequest) {
	destinations, err := listDestinations(req)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}

	logger.InfoContext(r.Context(), "destinations listed",
		slog.Int("count", len(destinations)),
		slog.Any("filters", req),
	)

	writeJSON(w, http.StatusOK, map[string]any{
		"count":        len(destinations),
		"filters_used": req,
		"destinations": destinations,
	})
}

func writeShortlistResponse(w http.ResponseWriter, r *http.Request, req ShortlistDestinationsRequest) {
	results, err := shortlistDestinations(req)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}

	logger.InfoContext(r.Context(), "shortlist search completed",
		slog.String("query", req.Query),
		slog.Int("results", len(results)),
	)

	writeJSON(w, http.StatusOK, map[string]any{
		"query":   req.Query,
		"count":   len(results),
		"results": results,
	})
}

func readDestinations() (DestinationsDB, error) {
	bs, err := os.ReadFile("data/destinations.json")
	if err != nil {
		return DestinationsDB{}, err
	}
	var db DestinationsDB
	if err := json.Unmarshal(bs, &db); err != nil {
		return DestinationsDB{}, err
	}
	return db, nil
}

func listDestinations(req ListDestinationsRequest) ([]Destination, error) {
	if err := validateVibes("vibes", req.Vibes); err != nil {
		return nil, err
	}
	if err := validateBudget(req.BudgetLevel); err != nil {
		return nil, err
	}

	limit := req.Limit
	if limit <= 0 {
		limit = 10
	}

	reqVibes := normalizeSlice(req.Vibes)
	reqActivities := normalizeSlice(req.Activities)
	budget := strings.ToLower(strings.TrimSpace(req.BudgetLevel))

	type scored struct {
		D     Destination
		Score int
	}
	var out []scored

	for _, d := range cachedDestinations.Destinations {
		if budget != "" && d.BudgetLevel != budget {
			continue
		}
		if req.RequireAllVibes {
			if !allMatch(d.Vibes, reqVibes) {
				continue
			}
		} else if !anyMatch(d.Vibes, reqVibes) {
			continue
		}
		if len(reqActivities) > 0 && !anyMatch(d.Activities, reqActivities) {
			continue
		}

		vset := makeSet(d.Vibes)
		aset := makeSet(d.Activities)
		score := 0
		for _, v := range reqVibes {
			if _, ok := vset[v]; ok {
				score += 10
			}
		}
		if len(reqVibes) == 0 {
			score += 1
		}
		for _, a := range reqActivities {
			if _, ok := aset[a]; ok {
				score += 2
			}
		}

		out = append(out, scored{D: d, Score: score})
	}

	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Score != out[j].Score {
			return out[i].Score > out[j].Score
		}
		if budgetRank(out[i].D.BudgetLevel) != budgetRank(out[j].D.BudgetLevel) {
			return budgetRank(out[i].D.BudgetLevel) < budgetRank(out[j].D.BudgetLevel)
		}
		return out[i].D.Name < out[j].D.Name
	})

	destinations := make([]Destination, 0, limit)
	for i := 0; i < len(out) && len(destinations) < limit; i++ {
		destinations = append(destinations, out[i].D)
	}
	return destinations, nil
}

func shortlistDestinations(req ShortlistDestinationsRequest) ([]Destination, error) {
	q := strings.ToLower(strings.TrimSpace(req.Query))
	if q == "" {
		return nil, errors.New("query is required")
	}

	limit := req.Limit
	if limit <= 0 {
		limit = 5
	}

	type hit struct {
		D     Destination
		Score int
	}
	var hits []hit

	for _, d := range cachedDestinations.Destinations {
		score := 0
		if strings.Contains(strings.ToLower(d.Name), q) {
			score += 5
		}
		if strings.Contains(strings.ToLower(d.Country), q) {
			score += 3
		}
		if strings.Contains(strings.ToLower(d.Region), q) {
			score += 2
		}
		for _, v := range d.Vibes {
			if strings.Contains(strings.ToLower(v), q) {
				score += 2
			}
		}
		for _, a := range d.Activities {
			if strings.Contains(strings.ToLower(a), q) {
				score += 1
			}
		}
		for _, code := range d.AirportIATACodes {
			if strings.Contains(strings.ToLower(code), q) {
				score += 4
			}
		}
		if score > 0 {
			hits = append(hits, hit{D: d, Score: score})
		}
	}

	sort.SliceStable(hits, func(i, j int) bool {
		if hits[i].Score != hits[j].Score {
			return hits[i].Score > hits[j].Score
		}
		return hits[i].D.Name < hits[j].D.Name
	})

	results := make([]Destination, 0, limit)
	for i := 0; i < len(hits) && len(results) < limit; i++ {
		results = append(results, hits[i].D)
	}
	return results, nil
}

func findDestination(id string) (Destination, bool) {
	id = strings.ToLower(strings.TrimSpace(id))
	for _, d := range cachedDestinations.Destinations {
		if d.ID == id {
			return d, true
		}
	}
	return Destination{}, false
}

func validateVibes(label string, vibes []string) error {
	var invalid []string
	for _, v := range normalizeSlice(vibes) {
		if _, ok := allowedVibes[v]; !ok {
			invalid = append(invalid, v)
		}
	}
	if len(invalid) > 0 {
		return fmt.Errorf("invalid %s: %s", label, strings.Join(invalid, ", "))
	}
	return nil
}

func validateBudget(v string) error {
	v = strings.ToLower(strings.TrimSpace(v))
	if v == "" {
		return nil
	}
	if _, ok := allowedBudget[v]; ok {
		return nil
	}
	return fmt.Errorf("invalid budget_level: %s", v)
}

func makeSet(ss []string) map[string]struct{} {
	m := make(map[string]struct{}, len(ss))
	for _, s := range normalizeSlice(ss) {
		m[s] = struct{}{}
	}
	return m
}

func anyMatch(haystack []string, needles []string) bool {
	if len(needles) == 0 {
		return true
	}
	set := makeSet(haystack)
	for _, n := range normalizeSlice(needles) {
		if _, ok := set[n]; ok {
			return true
		}
	}
	return false
}

func allMatch(haystack []string, needles []string) bool {
	if len(needles) == 0 {
		return true
	}
	set := makeSet(haystack)
	for _, n := range normalizeSlice(needles) {
		if _, ok := set[n]; !ok {
			return false
		}
	}
	return true
}

func budgetRank(b string) int {
	switch strings.ToLower(strings.TrimSpace(b)) {
	case "low":
		return 1
	case "mid":
		return 2
	case "high":
		return 3
	default:
		return 99
	}
}

func normalizeSlice(ss []string) []string {
	out := make([]string, 0, len(ss))
	for _, s := range ss {
		s = strings.ToLower(strings.TrimSpace(s))
		if s != "" {
			out = append(out, s)
		}
	}
	return out
}

func splitCSV(s string) []string {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	return strings.Split(s, ",")
}

func parseInt(s string) int {
	i, _ := strconv.Atoi(strings.TrimSpace(s))
	return i
}

func parseBool(s string) bool {
	v, _ := strconv.ParseBool(strings.TrimSpace(s))
	return v
}

func withJSON(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		next.ServeHTTP(w, r)
	})
}

func withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization, traceparent, tracestate, baggage")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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

func writeError(w http.ResponseWriter, status int, code string, message string) {
	writeJSON(w, status, map[string]any{
		"error": map[string]any{
			"code":    code,
			"message": message,
		},
	})
}

func getenv(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
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

func getenvWithFallback(primary, fallback, def string) string {
	if v := strings.TrimSpace(os.Getenv(primary)); v != "" {
		return stripURLScheme(v)
	}
	if v := strings.TrimSpace(os.Getenv(fallback)); v != "" {
		return stripURLScheme(v)
	}
	return def
}

func stripURLScheme(s string) string {
	s = strings.TrimPrefix(s, "http://")
	s = strings.TrimPrefix(s, "https://")
	return s
}
