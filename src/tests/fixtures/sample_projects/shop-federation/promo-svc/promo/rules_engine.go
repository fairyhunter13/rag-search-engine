package promo

import "fmt"

// RuleError captures a single rule violation with its name and message.
type RuleError struct {
	Rule    string
	Message string
}

func (e RuleError) Error() string {
	return fmt.Sprintf("%s: %s", e.Rule, e.Message)
}

// RuleFunc is a function that validates a RuleContext and returns an error on violation.
type RuleFunc func(ctx RuleContext) error

// RuleContext carries all inputs a rule might need during promo validation.
type RuleContext struct {
	UserID         string
	PromoCode      string
	OrderTotal     float64
	PriorOrders    int
	AlreadyClaimed bool
	TotalUsages    int
	AppliedCoupons []string
	Details        *PromoDetails
}

// NewRuleContext constructs a fully populated RuleContext.
func NewRuleContext(
	userID, code string,
	orderTotal float64,
	priorOrders int,
	alreadyClaimed bool,
	totalUsages int,
	applied []string,
	details *PromoDetails,
) RuleContext {
	return RuleContext{
		UserID:         userID,
		PromoCode:      code,
		OrderTotal:     orderTotal,
		PriorOrders:    priorOrders,
		AlreadyClaimed: alreadyClaimed,
		TotalUsages:    totalUsages,
		AppliedCoupons: applied,
		Details:        details,
	}
}

// RuleEntry is a named rule registered with a RuleEngine.
type RuleEntry struct {
	Name string
	Fn   RuleFunc
}

// RuleEngine executes an ordered list of validation rules against a RuleContext.
// Rules execute in registration order; the engine collects all violations.
type RuleEngine struct {
	rules []RuleEntry
}

// NewRuleEngine creates an empty RuleEngine ready for rule registration.
func NewRuleEngine() *RuleEngine {
	return &RuleEngine{}
}

// Register adds a named rule to the engine.
func (e *RuleEngine) Register(name string, fn RuleFunc) {
	e.rules = append(e.rules, RuleEntry{Name: name, Fn: fn})
}

// RunAll executes every rule and returns all violations (empty slice on success).
func (e *RuleEngine) RunAll(ctx RuleContext) []RuleError {
	var errs []RuleError
	for _, r := range e.rules {
		if err := r.Fn(ctx); err != nil {
			errs = append(errs, RuleError{Rule: r.Name, Message: err.Error()})
		}
	}
	return errs
}

// HasViolations returns true if any registered rule is violated by ctx.
func (e *RuleEngine) HasViolations(ctx RuleContext) bool {
	return len(e.RunAll(ctx)) > 0
}

// DefaultRuleEngine builds the standard promo validation rule set.
func DefaultRuleEngine() *RuleEngine {
	e := NewRuleEngine()
	e.Register("eligibility", func(ctx RuleContext) error {
		return DiscountEligibilityRule(ctx.UserID, ctx.PromoCode, ctx.PriorOrders, ctx.AlreadyClaimed)
	})
	e.Register("stacking", func(ctx RuleContext) error {
		return CouponStackingLimitRule(ctx.AppliedCoupons, ctx.PromoCode)
	})
	e.Register("window", func(ctx RuleContext) error {
		if ctx.Details == nil {
			return fmt.Errorf("promo details required for window check")
		}
		return PromoWindowRule(ctx.Details, clockNow())
	})
	e.Register("min_order", func(ctx RuleContext) error {
		if ctx.Details == nil {
			return fmt.Errorf("promo details required for min order check")
		}
		return MinOrderRule(ctx.Details, ctx.OrderTotal)
	})
	e.Register("usage_cap", func(ctx RuleContext) error {
		if ctx.Details == nil {
			return fmt.Errorf("promo details required for usage cap check")
		}
		return MaxUsageRule(ctx.Details, ctx.TotalUsages)
	})
	e.Register("reservation", func(ctx RuleContext) error {
		existing := GetCurrentReservation(ctx.PromoCode)
		return ReservationConflictRule(ctx.PromoCode, ctx.UserID, existing)
	})
	e.Register("order_value_cap", func(ctx RuleContext) error {
		if ctx.Details == nil {
			return fmt.Errorf("promo details required for order value cap check")
		}
		return OrderValueCapRule(ctx.Details, ctx.OrderTotal)
	})
	return e
}
