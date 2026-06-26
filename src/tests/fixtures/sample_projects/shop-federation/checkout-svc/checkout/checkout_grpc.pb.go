package checkout

import "google.golang.org/grpc"

// CheckoutServiceClient is the client API for CheckoutService.
type CheckoutServiceClient interface {
	PlaceOrder(userID, promoCode string) (*OrderResult, error)
	GetOrderStatus(orderID string) (string, error)
}

type checkoutServiceClient struct{ cc *grpc.ClientConn }

func NewCheckoutServiceClient(cc *grpc.ClientConn) CheckoutServiceClient {
	return &checkoutServiceClient{cc}
}

func (c *checkoutServiceClient) PlaceOrder(userID, promoCode string) (*OrderResult, error) {
	return nil, nil
}
func (c *checkoutServiceClient) GetOrderStatus(orderID string) (string, error) { return "", nil }

// CheckoutServiceServer is the server API for CheckoutService.
type CheckoutServiceServer interface {
	PlaceOrder(userID, promoCode string) (*OrderResult, error)
	GetOrderStatus(orderID string) (string, error)
}

func RegisterCheckoutServiceServer(s *grpc.Server, srv CheckoutServiceServer) {}

// OrderResult contains the result of a placed order.
type OrderResult struct {
	OrderID  string
	Total    float64
	Discount float64
	Status   string
}
