package promo

import "google.golang.org/grpc"

// PromoServiceClient is the client API for PromoService.
type PromoServiceClient interface {
	ApplyPromo(userID, code string, orderTotal float64) (float64, error)
	ValidatePromo(code string) (bool, error)
	GetPromoDetails(code string) (*PromoDetails, error)
}

type promoServiceClient struct{ cc *grpc.ClientConn }

func NewPromoServiceClient(cc *grpc.ClientConn) PromoServiceClient {
	return &promoServiceClient{cc}
}

func (c *promoServiceClient) ApplyPromo(userID, code string, orderTotal float64) (float64, error) {
	return 0, nil
}
func (c *promoServiceClient) ValidatePromo(code string) (bool, error)          { return true, nil }
func (c *promoServiceClient) GetPromoDetails(code string) (*PromoDetails, error) { return nil, nil }

// PromoServiceServer is the server API for PromoService.
type PromoServiceServer interface {
	ApplyPromo(userID, code string, orderTotal float64) (float64, error)
	ValidatePromo(code string) (bool, error)
	GetPromoDetails(code string) (*PromoDetails, error)
}

func RegisterPromoServiceServer(s *grpc.Server, srv PromoServiceServer) {}

// PromoDetails holds the configuration for a promotional offer.
type PromoDetails struct {
	Code          string
	DiscountPct   float64
	MinOrder      float64
	MaxOrderValue float64
	MaxUses       int
	ValidFrom     int64
	ValidUntil    int64
}
