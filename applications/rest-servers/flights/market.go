package main

// A moving fare market, off by default.
//
// By default a fare is a pure function of route and date, as it always was. Two opt-in settings
// make fares move over time so something can watch them:
//
//	FARE_TICK_SECONDS=30     every 30 seconds the market re-rolls: each route/date gets a new
//	                         deterministic multiplier of roughly +/-12% around the base fare.
//	ENABLE_FARE_SHOCKS=true  POST /v1/fare-shocks {"from","to","date","pct"} moves one route/date by
//	                         pct percent on top of the market (negative = a price drop) until DELETE
//	                         /v1/fare-shocks clears them. For demos and tests.

import (
	"encoding/json"
	"hash/fnv"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

var (
	fareTickSeconds = envInt("FARE_TICK_SECONDS", 0)
	fareShocks      sync.Map // "FROM|TO|DATE" -> percent (float64)
)

func envInt(key string, def int) int {
	if v, err := strconv.Atoi(strings.TrimSpace(os.Getenv(key))); err == nil && v >= 0 {
		return v
	}
	return def
}

func shockKey(from, to, date string) string { return from + "|" + to + "|" + date }

// marketMultiplier is 1.0 unless FARE_TICK_SECONDS or a fare shock applies.
func marketMultiplier(from, to string, d time.Time, now time.Time) float64 {
	mult := 1.0
	if fareTickSeconds > 0 {
		h := fnv.New64a()
		_, _ = h.Write([]byte(shockKey(from, to, d.Format("2006-01-02")) + "|" + strconv.FormatInt(now.Unix()/int64(fareTickSeconds), 10)))
		unit := float64(h.Sum64()%100000) / 100000.0 // 0..1
		mult += (unit - 0.5) * 0.24
	}
	if pct, ok := fareShocks.Load(shockKey(from, to, d.Format("2006-01-02"))); ok {
		mult *= 1 + pct.(float64)/100
	}
	return mult
}

type fareShockRequest struct {
	From string  `json:"from"`
	To   string  `json:"to"`
	Date string  `json:"date"`
	Pct  float64 `json:"pct"`
}

func handleFareShocks(w http.ResponseWriter, r *http.Request) {
	if os.Getenv("ENABLE_FARE_SHOCKS") != "true" {
		writeError(w, http.StatusNotFound, "not_found", "fare shocks are disabled (set ENABLE_FARE_SHOCKS=true)")
		return
	}
	switch r.Method {
	case http.MethodPost:
		var req fareShockRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeError(w, http.StatusBadRequest, "bad_request", "invalid JSON body")
			return
		}
		from, to := strings.ToUpper(strings.TrimSpace(req.From)), strings.ToUpper(strings.TrimSpace(req.To))
		if _, err := parseDate(req.Date); err != nil || len(from) != 3 || len(to) != 3 || req.Pct <= -90 || req.Pct > 500 {
			writeError(w, http.StatusBadRequest, "bad_request", "need from, to (IATA), date (YYYY-MM-DD) and pct between -90 and 500")
			return
		}
		fareShocks.Store(shockKey(from, to, req.Date), req.Pct)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "from": from, "to": to, "date": req.Date, "pct": req.Pct})
	case http.MethodDelete:
		fareShocks.Range(func(k, _ any) bool { fareShocks.Delete(k); return true })
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	default:
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use POST or DELETE")
	}
}
