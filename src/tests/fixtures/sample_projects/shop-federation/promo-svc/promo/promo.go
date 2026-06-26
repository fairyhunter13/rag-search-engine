package promo

import (
	"fmt"
	"sync"
	"time"
)

// promoStorage is the in-memory store for promo definitions and usage counts.
type promoStorage struct {
	mu     sync.RWMutex
	promos map[string]*PromoDetails
	usages map[string]map[string]int // promoCode -> userID -> count
	total  map[string]int             // promoCode -> global count
}

var store = &promoStorage{
	promos: make(map[string]*PromoDetails),
	usages: make(map[string]map[string]int),
	total:  make(map[string]int),
}

// clockNow is a variable so tests can override the wall clock.
var clockNow = time.Now

// RegisterPromo adds a new promo definition to the store.
func RegisterPromo(details *PromoDetails) error {
	if details.Code == "" {
		return fmt.Errorf("promo code required")
	}
	if details.ValidUntil <= details.ValidFrom {
		return fmt.Errorf("ValidUntil must be after ValidFrom for promo %s", details.Code)
	}
	if details.DiscountPct <= 0 || details.DiscountPct > 100 {
		return fmt.Errorf("DiscountPct must be between 0 and 100 for promo %s", details.Code)
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	store.promos[details.Code] = details
	return nil
}

// lookupPromo retrieves a promo by code (nil if not found).
func lookupPromo(code string) *PromoDetails {
	store.mu.RLock()
	defer store.mu.RUnlock()
	return store.promos[code]
}

// hasAlreadyClaimed reports whether userID claimed this promo in the current cycle.
func hasAlreadyClaimed(promoCode, userID string) bool {
	store.mu.RLock()
	defer store.mu.RUnlock()
	m, ok := store.usages[promoCode]
	if !ok {
		return false
	}
	return m[userID] > 0
}

// totalUsageCount returns the global usage count for a promo.
func totalUsageCount(promoCode string) int {
	store.mu.RLock()
	defer store.mu.RUnlock()
	return store.total[promoCode]
}

// recordUsage increments the usage counters for a promo+user pair.
func recordUsage(promoCode, userID string) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.usages[promoCode] == nil {
		store.usages[promoCode] = make(map[string]int)
	}
	store.usages[promoCode][userID]++
	store.total[promoCode]++
}

type promoServiceImpl struct{}

func NewPromoServer() PromoServiceServer {
	return &promoServiceImpl{}
}

func (p *promoServiceImpl) ApplyPromo(userID, code string, orderTotal float64) (float64, error) {
	claimed := hasAlreadyClaimed(code, userID)
	usages := totalUsageCount(code)
	discount, err := FulfillmentWorkflow(userID, code, orderTotal, 1, claimed, usages)
	if err != nil {
		return 0, err
	}
	recordUsage(code, userID)
	return discount, nil
}

func (p *promoServiceImpl) ValidatePromo(code string) (bool, error) {
	details := lookupPromo(code)
	if details == nil {
		return false, fmt.Errorf("promo %s not found", code)
	}
	if err := PromoWindowRule(details, clockNow()); err != nil {
		return false, err
	}
	usages := totalUsageCount(code)
	if err := MaxUsageRule(details, usages); err != nil {
		return false, err
	}
	return true, nil
}

func (p *promoServiceImpl) GetPromoDetails(code string) (*PromoDetails, error) {
	details := lookupPromo(code)
	if details == nil {
		return nil, fmt.Errorf("promo %s not found", code)
	}
	return details, nil
}
