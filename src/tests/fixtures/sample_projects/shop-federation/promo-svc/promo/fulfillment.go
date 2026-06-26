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

// FulfillmentWorkflow runs the full promo fulfillment pipeline in order:
//  1. Check discount eligibility (user has prior orders; not already claimed)
//  2. Validate the promo window (time-bounded campaign)
//  3. Enforce minimum order value
//  4. Check global usage cap
//  5. Reserve the promo slot to prevent concurrent double-application
//  6. Compute and return the discount amount
//
// The slot is always released (via defer) whether or not step 6 succeeds.
func FulfillmentWorkflow(
	userID, code string,
	orderTotal float64,
	priorOrders int,
	alreadyClaimed bool,
	totalUsages int,
) (float64, error) {
	if err := DiscountEligibilityRule(userID, code, priorOrders, alreadyClaimed); err != nil {
		return 0, fmt.Errorf("eligibility: %w", err)
	}
	details := lookupPromo(code)
	if details == nil {
		return 0, fmt.Errorf("promo %s not found", code)
	}
	if err := PromoWindowRule(details, clockNow()); err != nil {
		return 0, fmt.Errorf("window: %w", err)
	}
	if err := MinOrderRule(details, orderTotal); err != nil {
		return 0, fmt.Errorf("min order: %w", err)
	}
	if err := MaxUsageRule(details, totalUsages); err != nil {
		return 0, fmt.Errorf("usage cap: %w", err)
	}
	if err := ReservePromo(code, userID); err != nil {
		return 0, fmt.Errorf("reservation: %w", err)
	}
	defer ReleasePromo(code, userID)
	return computeDiscount(orderTotal, details.DiscountPct), nil
}
