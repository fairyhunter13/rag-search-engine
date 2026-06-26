package promo

import (
	"fmt"
	"sync"
)

// reservationLedger tracks which user currently holds each promo slot during checkout.
type reservationLedger struct {
	mu   sync.Mutex
	held map[string]string // promoCode -> userID
}

var slotLedger = &reservationLedger{held: make(map[string]string)}

// ReservePromo locks a promo slot for a user during the checkout flow.
// It runs the ReservationConflictRule before committing the slot to prevent races.
func ReservePromo(promoCode, userID string) error {
	slotLedger.mu.Lock()
	defer slotLedger.mu.Unlock()
	existing := slotLedger.held[promoCode]
	if err := ReservationConflictRule(promoCode, userID, existing); err != nil {
		return err
	}
	slotLedger.held[promoCode] = userID
	return nil
}

// ReleasePromo releases a held promo slot once checkout completes or fails.
func ReleasePromo(promoCode, userID string) {
	slotLedger.mu.Lock()
	defer slotLedger.mu.Unlock()
	if slotLedger.held[promoCode] == userID {
		delete(slotLedger.held, promoCode)
	}
}

// GetCurrentReservation returns the userID currently holding the promo slot, or "" if free.
func GetCurrentReservation(code string) string {
	slotLedger.mu.RLock()
	defer slotLedger.mu.RUnlock()
	return slotLedger.held[code]
}

// FulfillmentWorkflow runs the full promo fulfillment pipeline in order:
//  1. Run all validation rules via DefaultRuleEngine
//  2. Reserve the promo slot to prevent concurrent double-application
//  3. Compute and return the discount amount
//
// The slot is always released (via defer) whether or not step 3 succeeds.
func FulfillmentWorkflow(
	userID, code string,
	orderTotal float64,
	priorOrders int,
	alreadyClaimed bool,
	totalUsages int,
) (float64, error) {
	details := lookupPromo(code)
	if details == nil {
		return 0, fmt.Errorf("promo %s not found", code)
	}
	ctx := NewRuleContext(userID, code, orderTotal, priorOrders, alreadyClaimed, totalUsages, nil, details)
	engine := DefaultRuleEngine()
	if violations := engine.RunAll(ctx); len(violations) > 0 {
		return 0, fmt.Errorf("rule violation: %s", violations[0].Error())
	}
	if err := ReservePromo(code, userID); err != nil {
		return 0, fmt.Errorf("reservation: %w", err)
	}
	defer ReleasePromo(code, userID)
	return computeDiscount(orderTotal, details.DiscountPct), nil
}
