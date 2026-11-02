package main

import (
	"context"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	// otelhttp instruments inbound HTTP requests:
	// - extracts incoming trace context from headers (traceparent/tracestate)
	// - starts a server span for the request
	// - attaches that span to the request Context (r.Context())
	// - ends the span when the handler returns
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	// otelslog bridges slog to OTEL - logs are automatically correlated with traces
	"go.opentelemetry.io/contrib/bridges/otelslog"

	// Core OpenTelemetry APIs:
	"go.opentelemetry.io/otel"

	// Attributes (resource labels, span attrs, etc.)
	"go.opentelemetry.io/otel/attribute"

	// OTLP HTTP exporter (to grafana/otel-lgtm:4318) - metrics
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"

	// OTLP HTTP exporter (to grafana/otel-lgtm:4318) - logs
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"

	// OTLP HTTP exporter (to grafana/otel-lgtm:4318) - traces
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"

	// Propagation: W3C tracecontext + baggage for gateway and agent clients.
	"go.opentelemetry.io/otel/propagation"

	// SDK (log provider)
	sdklog "go.opentelemetry.io/otel/sdk/log"

	// SDK (meter provider)
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"

	// SDK (tracer provider, batching, sampling)
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	// Semconv (service.name)
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"

	// For debug header extraction logging
	"go.opentelemetry.io/otel/trace"
)

// Global logger - set in initLogging()
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
	otelHandler := otelslog.NewHandler("mcp-dice-roller", otelslog.WithLoggerProvider(logProvider))

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
			semconv.ServiceName(getenv("OTEL_SERVICE_NAME", "mcp-dice-roller")),
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
			semconv.ServiceName(getenv("OTEL_SERVICE_NAME", "mcp-dice-roller")),
			attribute.String("deployment.environment", getenv("OTEL_ENVIRONMENT", "dev")),
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

// getenvWithFallback checks primary env var, then fallback, then default.
// Follows OTEL convention: signal-specific > base endpoint > default.
// For OTLP endpoints, strips http:// or https:// since WithEndpoint() expects host:port.
func getenvWithFallback(primary, fallback, def string) string {
	if v := strings.TrimSpace(os.Getenv(primary)); v != "" {
		return stripURLScheme(v)
	}
	if v := strings.TrimSpace(os.Getenv(fallback)); v != "" {
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

//
// -------------------- tracing debug helpers --------------------
//

// debugIncomingPropagation logs whether trace headers are present and whether we extracted a valid remote SpanContext.
func debugIncomingPropagation(r *http.Request) {
	tp := r.Header.Get("traceparent")
	ts := r.Header.Get("tracestate")
	bg := r.Header.Get("baggage")

	ctx := otel.GetTextMapPropagator().Extract(r.Context(), propagation.HeaderCarrier(r.Header))
	sc := trace.SpanContextFromContext(ctx)

	logger.InfoContext(ctx, "incoming propagation headers",
		slog.String("traceparent", tp),
		slog.String("tracestate", ts),
		slog.String("baggage", bg),
		slog.Bool("extracted_valid", sc.IsValid()),
		slog.Bool("extracted_remote", sc.IsRemote()),
		slog.String("extracted_trace_id", sc.TraceID().String()),
		slog.String("extracted_span_id", sc.SpanID().String()),
	)
}

//
// -------------------- Tool implementation --------------------
//

type Output struct {
	Sides  int    `json:"sides" jsonschema:"the number of sides on the die"`
	Result int    `json:"result" jsonschema:"the result of the dice roll"`
	Debug  string `json:"debug" jsonschema:"debug information including the roll time"`
}

type HealthOutput struct {
	OK      bool   `json:"ok" jsonschema:"whether the service is healthy"`
	Service string `json:"service" jsonschema:"service name"`
	Time    string `json:"time" jsonschema:"current server time"`
}

func Health(
	ctx context.Context,
	req *mcp.CallToolRequest,
	input struct{},
) (*mcp.CallToolResult, HealthOutput, error) {
	return nil, HealthOutput{
		OK:      true,
		Service: "mcp-dice-roller",
		Time:    time.Now().UTC().Format(time.RFC3339),
	}, nil
}

func roll(sides int) func(context.Context, *mcp.CallToolRequest, struct{}) (*mcp.CallToolResult, Output, error) {
	return func(
		ctx context.Context,
		req *mcp.CallToolRequest,
		input struct{},
	) (*mcp.CallToolResult, Output, error) {
		result := rand.IntN(sides) + 1

		logger.InfoContext(ctx, "dice rolled",
			slog.Int("sides", sides),
			slog.Int("result", result),
		)

		return nil, Output{
			Sides:  sides,
			Result: result,
			Debug:  fmt.Sprintf("time: %s", time.Now().Format(time.RFC3339)),
		}, nil
	}
}

//
// -------------------- main() --------------------
//

func main() {
	ctx := context.Background()

	// Initialize logging first so errors during tracer init are logged
	logShutdown, err := initLogging(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "otel logging init failed: %v\n", err)
		os.Exit(1)
	}
	defer func() {
		_ = logShutdown(context.Background())
	}()

	// Initialize tracing
	traceShutdown, err := initTracer(ctx)
	if err != nil {
		logger.Error("otel tracing init failed", slog.Any("error", err))
		os.Exit(1)
	}
	defer func() {
		_ = traceShutdown(context.Background())
	}()

	metricShutdown, err := initMetrics(ctx)
	if err != nil {
		logger.Error("otel metrics init failed", slog.Any("error", err))
		os.Exit(1)
	}
	defer func() {
		_ = metricShutdown(context.Background())
	}()

	logger.Info("OpenTelemetry initialized (logs + traces + metrics)")
	logger.Info("service startup completed",
		slog.String("service", getenv("OTEL_SERVICE_NAME", "mcp-dice-roller")),
		slog.Bool("otel_logs_enabled", envBoolDefaultTrue("OTEL_LOGS_ENABLED")),
		slog.Bool("otel_traces_enabled", envBoolDefaultTrue("OTEL_TRACES_ENABLED")),
		slog.Bool("otel_propagation_enabled", envBoolDefaultTrue("OTEL_PROPAGATION_ENABLED")),
		slog.Bool("otel_metrics_enabled", envBoolDefaultTrue("OTEL_METRICS_ENABLED")),
	)

	// Create the MCP handler
	handler := mcp.NewStreamableHTTPHandler(
		func(*http.Request) *mcp.Server {
			server := mcp.NewServer(
				&mcp.Implementation{
					Name:    "dice-roller",
					Version: "v1.0.0",
				},
				nil,
			)

			mcp.AddTool(server, &mcp.Tool{
				Name:        "health",
				Description: "Health check for the dice roller MCP server",
			}, Health)

			mcp.AddTool(server, &mcp.Tool{
				Name:        "roll-2",
				Description: "Roll a 2-sided die (D2)",
			}, roll(2))

			mcp.AddTool(server, &mcp.Tool{
				Name:        "roll-6",
				Description: "Roll a 6-sided die (D6)",
			}, roll(6))

			mcp.AddTool(server, &mcp.Tool{
				Name:        "roll-20",
				Description: "Roll a 20-sided die (D20)",
			}, roll(20))

			return server
		},
		&mcp.StreamableHTTPOptions{},
	)

	// Debug wrapper (logs incoming propagation headers + extracted context)
	debugWrapper := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		debugIncomingPropagation(r)
		handler.ServeHTTP(w, r)
	})

	// Wrap with otelhttp for automatic server spans and trace extraction
	otelHandler := otelhttp.NewHandler(debugWrapper, "mcp.dice-roller")

	mux := http.NewServeMux()
	mux.Handle("/", otelHandler)

	// Health check endpoint
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"ok":true,"service":"mcp-dice-roller"}`)
	})

	srv := &http.Server{
		Addr:              ":8000",
		Handler:           mux,
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

	logger.Info("MCP dice-roller listening", slog.String("addr", ":8000"))

	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Error("server listen error", slog.Any("error", err))
		os.Exit(1)
	}

	logger.Info("server shutdown complete")
}
