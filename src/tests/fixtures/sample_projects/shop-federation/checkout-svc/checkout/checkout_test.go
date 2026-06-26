package checkout

import (
	"testing"

	cart "example.com/shop/cart-svc/cart"
)

func TestComputeTotal(t *testing.T) {
	items := []*cart.CartItem{
		{Price: 10.0, Quantity: 2},
		{Price: 5.0, Quantity: 1},
	}
	if computeTotal(items) != 25.0 {
		t.Errorf("expected 25.0, got %f", computeTotal(items))
	}
}

func TestComputeTotal_Empty(t *testing.T) {
	if computeTotal(nil) != 0 {
		t.Error("expected 0 for empty cart")
	}
}

func TestGenerateOrderID(t *testing.T) {
	id1 := generateOrderID()
	id2 := generateOrderID()
	if id1 == id2 {
		t.Error("order IDs must be unique")
	}
	if id1 == "" {
		t.Error("order ID must not be empty")
	}
}

func TestOrderStore_SaveAndLoad(t *testing.T) {
	s := &orderStore{orders: make(map[string]*OrderResult)}
	r := &OrderResult{OrderID: "ord-001", Total: 50.0, Status: "confirmed"}
	s.save(r)
	loaded, ok := s.load("ord-001")
	if !ok {
		t.Fatal("expected order to be found")
	}
	if loaded.Total != 50.0 {
		t.Errorf("expected total 50.0, got %f", loaded.Total)
	}
}

func TestOrderStore_NotFound(t *testing.T) {
	s := &orderStore{orders: make(map[string]*OrderResult)}
	_, ok := s.load("nonexistent")
	if ok {
		t.Error("expected not found")
	}
}

func TestValidateCartItems_Valid(t *testing.T) {
	items := []*cart.CartItem{{ProductID: "p1", Price: 10.0, Quantity: 1}}
	if err := validateCartItems(items); err != nil {
		t.Errorf("expected valid, got %v", err)
	}
}

func TestValidateCartItems_ZeroPrice(t *testing.T) {
	if validateCartItems([]*cart.CartItem{{ProductID: "p1", Price: 0, Quantity: 1}}) == nil {
		t.Error("expected error for zero price")
	}
}

func TestValidateCartItems_ZeroQty(t *testing.T) {
	if validateCartItems([]*cart.CartItem{{ProductID: "p1", Price: 10.0, Quantity: 0}}) == nil {
		t.Error("expected error for zero quantity")
	}
}

func TestEstimateShipping_Empty(t *testing.T) {
	if estimateShipping(nil) != 0 {
		t.Error("expected 0 shipping for empty cart")
	}
}

func TestEstimateShipping_Items(t *testing.T) {
	items := []*cart.CartItem{{}, {}, {}}
	if estimateShipping(items) != 5.0+float64(3)*0.5 {
		t.Error("unexpected shipping amount")
	}
}

func TestPricingResult_Total(t *testing.T) {
	pr := PricingResult{Subtotal: 100.0, Discount: 10.0, Shipping: 5.0, Total: 95.0}
	if pr.Total != 95.0 {
		t.Errorf("expected total 95.0, got %f", pr.Total)
	}
}

func TestOrderResult_Confirmed(t *testing.T) {
	r := &OrderResult{OrderID: "ord-001", Status: "confirmed"}
	if r.Status != "confirmed" {
		t.Errorf("expected confirmed, got %s", r.Status)
	}
}
