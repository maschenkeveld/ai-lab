package main

// Holds, confirmation, cancellation and idempotency for bookings.
//
// A booking created with hold=true is reserved but not confirmed: it carries status "held" and an
// expiry. Confirming a held booking makes it "scheduled" (the existing confirmed state); cancelling
// makes it "cancelled"; a hold that is not confirmed in time becomes "expired". An idempotency key
// makes a retried create return the booking the first attempt made instead of creating a second one.
// Requests without hold or idempotency_key behave exactly as before.

import (
	"context"
	"database/sql"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

const (
	statusHeld      = "held"
	statusCancelled = "cancelled"
	statusExpired   = "expired"

	defaultHoldSeconds = 900
	maxHoldSeconds     = 7 * 24 * 3600
)

// migrateHolds adds the hold columns to a database created before this feature existed.
func migrateHolds(db *sql.DB) error {
	for _, stmt := range []string{
		`ALTER TABLE bookings ADD COLUMN hold_expires_at_ts INTEGER NOT NULL DEFAULT 0`,
		`ALTER TABLE bookings ADD COLUMN idempotency_key TEXT`,
	} {
		if _, err := db.Exec(stmt); err != nil && !strings.Contains(strings.ToLower(err.Error()), "duplicate column") {
			return err
		}
	}
	_, err := db.Exec(`CREATE UNIQUE INDEX IF NOT EXISTS bookings_idempotency_key
		ON bookings (idempotency_key) WHERE idempotency_key IS NOT NULL`)
	return err
}

type heldBooking struct {
	Booking
	HoldExpiresAtTS int64 `json:"hold_expires_at_ts,omitempty"`
	IdempotentReply bool  `json:"idempotent_replay,omitempty"`
}

const heldColumns = `booking_code, flight_number, status, name, from_iata, to_iata, date, created_at, created_at_ts, hold_expires_at_ts`

func scanHeld(row interface{ Scan(...any) error }) (heldBooking, error) {
	var b heldBooking
	err := row.Scan(&b.BookingCode, &b.FlightNumber, &b.Status, &b.Name, &b.From, &b.To, &b.Date,
		&b.CreatedAt, &b.CreatedAtTS, &b.HoldExpiresAtTS)
	return b, err
}

// load returns a booking, first turning an overdue hold into "expired".
func (s *server) load(ctx context.Context, code string) (heldBooking, error) {
	_, _ = s.db.ExecContext(ctx, `UPDATE bookings SET status = ? WHERE booking_code = ? AND status = ?
		AND hold_expires_at_ts > 0 AND hold_expires_at_ts < ?`, statusExpired, code, statusHeld, time.Now().Unix())
	return scanHeld(s.db.QueryRowContext(ctx, `SELECT `+heldColumns+` FROM bookings WHERE booking_code = ?`, code))
}

// createHeldBooking handles POST /v1/bookings when hold or idempotency_key is set.
func (s *server) createHeldBooking(w http.ResponseWriter, r *http.Request, req CreateBookingRequest) {
	ctx := r.Context()
	key := strings.TrimSpace(req.IdempotencyKey)
	if key != "" {
		var code string
		err := s.db.QueryRowContext(ctx, `SELECT booking_code FROM bookings WHERE idempotency_key = ?`, key).Scan(&code)
		if err == nil {
			existing, lerr := s.load(ctx, code)
			if lerr == nil {
				existing.IdempotentReply = true
				logger.InfoContext(ctx, "idempotent replay", slog.String("booking_code", code))
				writeJSON(w, http.StatusOK, map[string]any{"booking": existing})
				return
			}
		}
	}

	b, err := validateBooking(req)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	var expires int64
	if req.Hold {
		secs := req.HoldSeconds
		if secs <= 0 {
			secs = defaultHoldSeconds
		}
		if secs > maxHoldSeconds {
			secs = maxHoldSeconds
		}
		b.Status = statusHeld
		expires = time.Now().Unix() + int64(secs)
	}
	var keyArg any
	if key != "" {
		keyArg = key
	}

	dbCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	for attempt := 0; attempt < 10; attempt++ {
		code, perr := generatePNR()
		if perr != nil {
			writeError(w, http.StatusInternalServerError, "internal_error", "failed to generate booking code")
			return
		}
		b.BookingCode = code
		_, err = s.db.ExecContext(dbCtx, `INSERT INTO bookings
			(booking_code, flight_number, status, name, from_iata, to_iata, date, created_at, created_at_ts,
			 hold_expires_at_ts, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			b.BookingCode, b.FlightNumber, b.Status, b.Name, b.From, b.To, b.Date, b.CreatedAt, b.CreatedAtTS, expires, keyArg)
		if err == nil {
			logger.InfoContext(ctx, "booking created", slog.String("booking_code", b.BookingCode),
				slog.String("status", b.Status), slog.Bool("idempotent", key != ""))
			writeJSON(w, http.StatusOK, map[string]any{"booking": heldBooking{Booking: b, HoldExpiresAtTS: expires}})
			return
		}
		if key != "" && strings.Contains(strings.ToLower(err.Error()), "idempotency_key") {
			// A concurrent request with the same key won the race: return its booking.
			var existingCode string
			if qerr := s.db.QueryRowContext(ctx, `SELECT booking_code FROM bookings WHERE idempotency_key = ?`, key).Scan(&existingCode); qerr == nil {
				if existing, lerr := s.load(ctx, existingCode); lerr == nil {
					existing.IdempotentReply = true
					writeJSON(w, http.StatusOK, map[string]any{"booking": existing})
					return
				}
			}
		}
		if isUniqueConstraintErr(err) {
			continue
		}
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	writeError(w, http.StatusInternalServerError, "db_error", "could not generate a unique booking code")
}

func (s *server) getBooking(w http.ResponseWriter, r *http.Request) {
	b, err := s.load(r.Context(), r.PathValue("code"))
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "not_found", "no such booking")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"booking": b})
}

// confirmBooking: held -> scheduled. Confirming a scheduled booking is a no-op; an expired or
// cancelled one cannot be confirmed (409).
func (s *server) confirmBooking(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	code := r.PathValue("code")
	b, err := s.load(ctx, code)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "not_found", "no such booking")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	switch b.Status {
	case statusScheduled:
		writeJSON(w, http.StatusOK, map[string]any{"booking": b})
	case statusHeld:
		if _, err := s.db.ExecContext(ctx, `UPDATE bookings SET status = ?, hold_expires_at_ts = 0 WHERE booking_code = ?`, statusScheduled, code); err != nil {
			writeError(w, http.StatusInternalServerError, "db_error", err.Error())
			return
		}
		b.Status, b.HoldExpiresAtTS = statusScheduled, 0
		logger.InfoContext(ctx, "booking confirmed", slog.String("booking_code", code))
		writeJSON(w, http.StatusOK, map[string]any{"booking": b})
	default:
		writeError(w, http.StatusConflict, "not_confirmable", "booking is "+b.Status+" and cannot be confirmed")
	}
}

// cancelBooking: held or scheduled -> cancelled. Cancelling a cancelled or expired booking is a no-op.
func (s *server) cancelBooking(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	code := r.PathValue("code")
	b, err := s.load(ctx, code)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "not_found", "no such booking")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "db_error", err.Error())
		return
	}
	if b.Status == statusHeld || b.Status == statusScheduled {
		if _, err := s.db.ExecContext(ctx, `UPDATE bookings SET status = ?, hold_expires_at_ts = 0 WHERE booking_code = ?`, statusCancelled, code); err != nil {
			writeError(w, http.StatusInternalServerError, "db_error", err.Error())
			return
		}
		b.Status, b.HoldExpiresAtTS = statusCancelled, 0
		logger.InfoContext(ctx, "booking cancelled", slog.String("booking_code", code))
	}
	writeJSON(w, http.StatusOK, map[string]any{"booking": b})
}

