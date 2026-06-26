package cart

import "testing"

func TestStore_AddItem(t *testing.T) {
	s := newStore()
	if err := s.add("u1", "p1", 2, 9.99); err != nil {
		t.Fatal(err)
	}
	items, _ := s.get("u1")
	if len(items) != 1 || items[0].Quantity != 2 {
		t.Errorf("expected 1 item qty=2, got %v", items)
	}
}

func TestStore_AddItem_Merge(t *testing.T) {
	s := newStore()
	s.add("u1", "p1", 1, 5.0)
	s.add("u1", "p1", 2, 5.0)
	items, _ := s.get("u1")
	if len(items) != 1 || items[0].Quantity != 3 {
		t.Errorf("expected merged qty=3, got %v", items)
	}
}

func TestStore_InvalidQty(t *testing.T) {
	s := newStore()
	if err := s.add("u1", "p1", 0, 5.0); err != ErrInvalidQty {
		t.Errorf("expected ErrInvalidQty, got %v", err)
	}
}

func TestStore_EmptyProduct(t *testing.T) {
	s := newStore()
	if err := s.add("u1", "", 1, 5.0); err != ErrEmptyProduct {
		t.Errorf("expected ErrEmptyProduct, got %v", err)
	}
}

func TestStore_Total(t *testing.T) {
	s := newStore()
	s.add("u1", "p1", 2, 10.0)
	s.add("u1", "p2", 1, 5.0)
	if s.total("u1") != 25.0 {
		t.Errorf("expected total 25.0, got %f", s.total("u1"))
	}
}

func TestStore_Clear(t *testing.T) {
	s := newStore()
	s.add("u1", "p1", 1, 1.0)
	s.clear("u1")
	items, _ := s.get("u1")
	if len(items) != 0 {
		t.Errorf("expected empty cart after clear, got %v", items)
	}
}

func TestStore_RemoveItem(t *testing.T) {
	s := newStore()
	s.add("u1", "p1", 1, 1.0)
	s.add("u1", "p2", 1, 2.0)
	removed := s.removeItem("u1", "p1")
	if !removed {
		t.Error("expected removeItem to return true")
	}
	items, _ := s.get("u1")
	if len(items) != 1 || items[0].ProductID != "p2" {
		t.Errorf("expected only p2 remaining, got %v", items)
	}
}

func TestStore_ItemCount(t *testing.T) {
	s := newStore()
	s.add("u1", "p1", 3, 1.0)
	s.add("u1", "p2", 2, 2.0)
	if s.itemCount("u1") != 5 {
		t.Errorf("expected item count 5, got %d", s.itemCount("u1"))
	}
}
