package cart

import "google.golang.org/grpc"

// CartServiceClient is the client API for CartService.
type CartServiceClient interface {
	AddItem(userID, productID string, qty int) error
	GetCart(userID string) ([]*CartItem, error)
	ClearCart(userID string) error
}

type cartServiceClient struct{ cc *grpc.ClientConn }

func NewCartServiceClient(cc *grpc.ClientConn) CartServiceClient {
	return &cartServiceClient{cc}
}

func (c *cartServiceClient) AddItem(userID, productID string, qty int) error   { return nil }
func (c *cartServiceClient) GetCart(userID string) ([]*CartItem, error)         { return nil, nil }
func (c *cartServiceClient) ClearCart(userID string) error                      { return nil }

// CartServiceServer is the server API for CartService.
type CartServiceServer interface {
	AddItem(userID, productID string, qty int) error
	GetCart(userID string) ([]*CartItem, error)
	ClearCart(userID string) error
}

func RegisterCartServiceServer(s *grpc.Server, srv CartServiceServer) {}

// CartItem represents a single item in a shopping cart.
type CartItem struct {
	ProductID string
	Quantity  int
	Price     float64
}
