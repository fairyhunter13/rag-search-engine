package checkout

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"

	"google.golang.org/grpc"
	cart "example.com/shop/cart-svc/cart"
	promo "example.com/shop/promo-svc/promo"
)

var ErrEmptyCart = errors.New("cart is empty")
var ErrInvalidUser = errors.New("user ID required")

var orderCounter uint64

type orderStore struct {
	mu     sync.RWMutex
	orders map[string]*OrderResult
}

var orders = &orderStore{orders: make(map[string]*OrderResult)}

func (s *orderStore) save(r *OrderResult) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.orders[r.OrderID] = r
}

func (s *orderStore) load(orderID string) (*OrderResult, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	r, ok := s.orders[orderID]
	return r, ok
}

type service struct {
	cartConn  *grpc.ClientConn
	promoConn *grpc.ClientConn
}

func NewService(cartConn, promoConn *grpc.ClientConn) CheckoutServiceServer {
	return &service{cartConn: cartConn, promoConn: promoConn}
}

func (s *service) PlaceOrder(userID, promoCode string) (*OrderResult, error) {
	if userID == "" {
		return nil, ErrInvalidUser
	}
	cartClient := cart.NewCartServiceClient(s.cartConn)
	items, err := cartClient.GetCart(userID)
	if err != nil {
		return nil, fmt.Errorf("get cart: %w", err)
	}
	if len(items) == 0 {
		return nil, ErrEmptyCart
	}
	total := computeTotal(items)
	discount := applyPromo(s.promoConn, userID, promoCode, total)
	finalTotal := total - discount
	orderID := generateOrderID()
	if err := cartClient.ClearCart(userID); err != nil {
		return nil, fmt.Errorf("clear cart: %w", err)
	}
	result := &OrderResult{
		OrderID:  orderID,
		Total:    finalTotal,
		Discount: discount,
		Status:   "confirmed",
	}
	orders.save(result)
	return result, nil
}

func (s *service) GetOrderStatus(orderID string) (string, error) {
	r, ok := orders.load(orderID)
	if !ok {
		return "", fmt.Errorf("order %s not found", orderID)
	}
	return r.Status, nil
}

func computeTotal(items []*cart.CartItem) float64 {
	var total float64
	for _, it := range items {
		total += it.Price * float64(it.Quantity)
	}
	return total
}

func applyPromo(conn *grpc.ClientConn, userID, code string, total float64) float64 {
	if code == "" || conn == nil {
		return 0
	}
	promoClient := promo.NewPromoServiceClient(conn)
	discount, err := promoClient.ApplyPromo(userID, code, total)
	if err != nil {
		return 0
	}
	return discount
}

func generateOrderID() string {
	n := atomic.AddUint64(&orderCounter, 1)
	return fmt.Sprintf("ord-%06d", n)
}

func RegisterHTTPHandlers(svc CheckoutServiceServer) {
	http.HandleFunc("/checkout/place", func(w http.ResponseWriter, r *http.Request) {
		userID := r.URL.Query().Get("user")
		promoCode := r.URL.Query().Get("promo")
		result, err := svc.PlaceOrder(userID, promoCode)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(result)
	})
	http.HandleFunc("/checkout/status", func(w http.ResponseWriter, r *http.Request) {
		orderID := r.URL.Query().Get("order")
		status, err := svc.GetOrderStatus(orderID)
		if err != nil {
			http.Error(w, err.Error(), http.StatusNotFound)
			return
		}
		fmt.Fprintf(w, status)
	})
}
