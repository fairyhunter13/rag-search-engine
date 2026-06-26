package cart

import (
	"errors"
	"sync"
)

var ErrNotFound = errors.New("cart not found")
var ErrInvalidQty = errors.New("quantity must be positive")
var ErrEmptyProduct = errors.New("product ID required")

type store struct {
	mu    sync.RWMutex
	carts map[string][]*CartItem
}

var globalStore = newStore()

func newStore() *store {
	return &store{carts: make(map[string][]*CartItem)}
}

func (s *store) add(userID, productID string, qty int, price float64) error {
	if qty <= 0 {
		return ErrInvalidQty
	}
	if productID == "" {
		return ErrEmptyProduct
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, it := range s.carts[userID] {
		if it.ProductID == productID {
			it.Quantity += qty
			return nil
		}
	}
	s.carts[userID] = append(s.carts[userID], &CartItem{
		ProductID: productID,
		Quantity:  qty,
		Price:     price,
	})
	return nil
}

func (s *store) get(userID string) ([]*CartItem, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	items, ok := s.carts[userID]
	if !ok {
		return []*CartItem{}, nil
	}
	return items, nil
}

func (s *store) clear(userID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.carts, userID)
}

func (s *store) total(userID string) float64 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var t float64
	for _, it := range s.carts[userID] {
		t += it.Price * float64(it.Quantity)
	}
	return t
}

func (s *store) itemCount(userID string) int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var n int
	for _, it := range s.carts[userID] {
		n += it.Quantity
	}
	return n
}

func (s *store) removeItem(userID, productID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	items := s.carts[userID]
	for i, it := range items {
		if it.ProductID == productID {
			s.carts[userID] = append(items[:i], items[i+1:]...)
			return true
		}
	}
	return false
}

type cartServiceImpl struct{}

func NewCartServer() CartServiceServer {
	return &cartServiceImpl{}
}

func (c *cartServiceImpl) AddItem(userID, productID string, qty int) error {
	return globalStore.add(userID, productID, qty, 0)
}

func (c *cartServiceImpl) GetCart(userID string) ([]*CartItem, error) {
	return globalStore.get(userID)
}

func (c *cartServiceImpl) ClearCart(userID string) error {
	globalStore.clear(userID)
	return nil
}

func CartTotal(userID string) float64 {
	return globalStore.total(userID)
}

func CartItemCount(userID string) int {
	return globalStore.itemCount(userID)
}
