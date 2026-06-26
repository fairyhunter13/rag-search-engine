package promo

import (
	"testing"
	"time"
)

func TestDiscountEligibilityRule(t *testing.T) {
	if err := DiscountEligibilityRule("u1", "PROMO10", 1, false); err != nil {
		t.Errorf("expected eligible, got %v", err)
	}
	if DiscountEligibilityRule("u1", "PROMO10", 0, false) == nil {
		t.Error("expected error for zero prior orders")
	}
	if DiscountEligibilityRule("u1", "PROMO10", 1, true) == nil {
		t.Error("expected error when already claimed")
	}
	if DiscountEligibilityRule("", "PROMO10", 1, false) == nil {
		t.Error("expected error for empty user ID")
	}
}

func TestCouponStackingLimitRule(t *testing.T) {
	if err := CouponStackingLimitRule([]string{}, "PROMO10"); err != nil {
		t.Errorf("expected ok, got %v", err)
	}
	if CouponStackingLimitRule([]string{"A", "B"}, "C") == nil {
		t.Error("expected error for max stack exceeded")
	}
	if CouponStackingLimitRule([]string{"A"}, "A") == nil {
		t.Error("expected error for duplicate coupon")
	}
}

func TestPromoWindowRule(t *testing.T) {
	now := time.Now()
	active := &PromoDetails{Code: "P", ValidFrom: now.Add(-time.Hour).Unix(), ValidUntil: now.Add(time.Hour).Unix()}
	if err := PromoWindowRule(active, now); err != nil {
		t.Errorf("expected active window, got %v", err)
	}
	expired := &PromoDetails{Code: "P", ValidFrom: now.Add(-2 * time.Hour).Unix(), ValidUntil: now.Add(-time.Minute).Unix()}
	if PromoWindowRule(expired, now) == nil {
		t.Error("expected expired error")
	}
	future := &PromoDetails{Code: "P", ValidFrom: now.Add(time.Hour).Unix(), ValidUntil: now.Add(2 * time.Hour).Unix()}
	if PromoWindowRule(future, now) == nil {
		t.Error("expected not-started error")
	}
}

func TestMinOrderRule(t *testing.T) {
	d := &PromoDetails{Code: "P", MinOrder: 50.0}
	if err := MinOrderRule(d, 100.0); err != nil {
		t.Errorf("expected ok, got %v", err)
	}
	if MinOrderRule(d, 30.0) == nil {
		t.Error("expected min order error")
	}
}

func TestReservationConflictRule(t *testing.T) {
	if ReservationConflictRule("P", "u1", "") != nil {
		t.Error("expected no conflict when slot free")
	}
	if ReservationConflictRule("P", "u1", "u1") != nil {
		t.Error("expected no conflict for same user")
	}
	if ReservationConflictRule("P", "u1", "u2") == nil {
		t.Error("expected conflict for different user")
	}
}

func TestMaxUsageRule(t *testing.T) {
	d := &PromoDetails{Code: "P", MaxUses: 10}
	if err := MaxUsageRule(d, 5); err != nil {
		t.Errorf("expected ok, got %v", err)
	}
	if MaxUsageRule(d, 10) == nil {
		t.Error("expected usage cap error")
	}
	unlimited := &PromoDetails{Code: "P", MaxUses: 0}
	if MaxUsageRule(unlimited, 9999) != nil {
		t.Error("expected no limit when MaxUses=0")
	}
}

func TestComputeDiscount(t *testing.T) {
	if computeDiscount(100.0, 10.0) != 10.0 {
		t.Errorf("expected 10, got %f", computeDiscount(100.0, 10.0))
	}
	if computeDiscount(100.0, 0) != 0 || computeDiscount(100.0, 101) != 0 {
		t.Error("expected 0 for invalid percentage")
	}
}

func TestReserveAndRelease(t *testing.T) {
	if err := ReservePromo("CODE_TEST", "u1"); err != nil {
		t.Fatalf("reserve: %v", err)
	}
	if ReservePromo("CODE_TEST", "u2") == nil {
		t.Error("expected conflict for second user")
	}
	ReleasePromo("CODE_TEST", "u1")
	if err := ReservePromo("CODE_TEST", "u2"); err != nil {
		t.Errorf("expected ok after release, got %v", err)
	}
	ReleasePromo("CODE_TEST", "u2")
}

func TestDefaultRuleEngine_Valid(t *testing.T) {
	now := time.Now()
	d := &PromoDetails{Code: "E2E", ValidFrom: now.Add(-time.Hour).Unix(),
		ValidUntil: now.Add(time.Hour).Unix(), DiscountPct: 10, MinOrder: 0, MaxUses: 0}
	ctx := NewRuleContext("u1", "E2E", 100.0, 1, false, 0, nil, d)
	e := DefaultRuleEngine()
	if e.HasViolations(ctx) {
		t.Errorf("expected no violations, got: %v", e.RunAll(ctx))
	}
}

func TestDefaultRuleEngine_Violation(t *testing.T) {
	now := time.Now()
	d := &PromoDetails{Code: "E2E", ValidFrom: now.Add(-time.Hour).Unix(),
		ValidUntil: now.Add(time.Hour).Unix(), DiscountPct: 10, MinOrder: 200, MaxUses: 0}
	ctx := NewRuleContext("u1", "E2E", 50.0, 1, false, 0, nil, d)
	e := DefaultRuleEngine()
	if !e.HasViolations(ctx) {
		t.Error("expected violation for order below min_order")
	}
}

func TestRuleEngine_Register(t *testing.T) {
	e := NewRuleEngine()
	e.Register("always_ok", func(_ RuleContext) error { return nil })
	if e.HasViolations(RuleContext{}) {
		t.Error("expected no violations")
	}
}
