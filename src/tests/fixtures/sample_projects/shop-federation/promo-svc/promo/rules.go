package promo

import (
	"fmt"
	"time"
)

// DiscountEligibilityRule checks whether a user qualifies for a promotional discount.
// A user is eligible only if their account has at least one prior order and has not
// already claimed the same promo code in the current billing cycle.
func DiscountEligibilityRule(userID, code string, priorOrders int, alreadyClaimed bool) error {
	if userID == "" {
		return fmt.Errorf("user ID required for promo eligibility check")
	}
	if priorOrders < 1 {
		return fmt.Errorf(
			"user must have at least one prior order to use promo %s (user %s has %d)",
			code, userID, priorOrders,
		)
	}
	if alreadyClaimed {
		return fmt.Errorf(
			"user %s has already claimed promo %s in the current billing cycle",
			userID, code,
		)
	}
	return nil
}

// CouponStackingLimitRule enforces that at most 2 promo codes may be applied to one order.
// If the user attempts to apply a third coupon, or the same coupon twice, the rule rejects it.
func CouponStackingLimitRule(appliedCoupons []string, newCode string) error {
	const maxStack = 2
	if len(appliedCoupons) >= maxStack {
		return fmt.Errorf(
			"cannot stack more than %d coupons per order (already applied: %v)",
			maxStack, appliedCoupons,
		)
	}
	for _, c := range appliedCoupons {
		if c == newCode {
			return fmt.Errorf("coupon %s is already applied to this order", newCode)
		}
	}
	return nil
}

// PromoWindowRule validates that the current time falls within the promotional window.
// Promos must not be applied before ValidFrom or after ValidUntil (inclusive boundaries).
func PromoWindowRule(details *PromoDetails, now time.Time) error {
	from := time.Unix(details.ValidFrom, 0)
	until := time.Unix(details.ValidUntil, 0)
	if now.Before(from) {
		return fmt.Errorf(
			"promo %s is not yet active (starts %s, now %s)",
			details.Code, from.Format(time.RFC3339), now.Format(time.RFC3339),
		)
	}
	if now.After(until) {
		return fmt.Errorf(
			"promo %s has expired (ended %s, now %s)",
			details.Code, until.Format(time.RFC3339), now.Format(time.RFC3339),
		)
	}
	return nil
}

// MinOrderRule enforces the minimum order value required to activate a promo code.
// Orders below the threshold are rejected to prevent abuse of high-value promos.
func MinOrderRule(details *PromoDetails, orderTotal float64) error {
	if orderTotal < details.MinOrder {
		return fmt.Errorf(
			"order total %.2f is below the minimum %.2f required for promo %s",
			orderTotal, details.MinOrder, details.Code,
		)
	}
	return nil
}

// ReservationConflictRule checks that no active reservation holds the promo slot.
// A promo slot is locked by the first user who begins checkout; if a different user
// already holds the lock, the request is rejected to prevent double-application.
func ReservationConflictRule(promoCode, requestingUserID, reservedBy string) error {
	if reservedBy == "" || reservedBy == requestingUserID {
		return nil
	}
	return fmt.Errorf(
		"promo %s is currently reserved by another user; try again shortly",
		promoCode,
	)
}

// MaxUsageRule ensures the promo has not exceeded its global usage cap.
func MaxUsageRule(details *PromoDetails, totalUsages int) error {
	if details.MaxUses > 0 && totalUsages >= details.MaxUses {
		return fmt.Errorf(
			"promo %s has reached its usage limit of %d",
			details.Code, details.MaxUses,
		)
	}
	return nil
}

// computeDiscount calculates the discount amount from a percentage and order total.
func computeDiscount(orderTotal, discountPct float64) float64 {
	if discountPct <= 0 || discountPct > 100 {
		return 0
	}
	return orderTotal * discountPct / 100
}
