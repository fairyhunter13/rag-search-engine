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
