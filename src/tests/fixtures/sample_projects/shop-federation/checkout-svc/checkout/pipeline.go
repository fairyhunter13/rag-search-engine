package checkout

import (
	"fmt"

	"google.golang.org/grpc"
	cart "example.com/shop/cart-svc/cart"
	promo "example.com/shop/promo-svc/promo"
)

// PricingResult holds the output of the pricing calculation step.
type PricingResult struct {
	Subtotal float64
	Discount float64
	Shipping float64
	Total    float64
}

// CheckoutPipeline orchestrates the multi-step order placement flow.
// Steps: validate request → fetch cart → calculate pricing → commit order.
type CheckoutPipeline struct {
	cartConn  *grpc.ClientConn
	promoConn *grpc.ClientConn
}

// NewPipeline creates a CheckoutPipeline wired to upstream gRPC connections.
func NewPipeline(cartConn, promoConn *grpc.ClientConn) *CheckoutPipeline {
	return &CheckoutPipeline{cartConn: cartConn, promoConn: promoConn}
}

// ValidateRequest checks that the request parameters are minimally valid.
func (p *CheckoutPipeline) ValidateRequest(userID string) error {
	if userID == "" {
		return ErrInvalidUser
	}
	return nil
}

// FetchCart retrieves and validates the user's current cart via gRPC.
func (p *CheckoutPipeline) FetchCart(userID string) ([]*cart.CartItem, error) {
	c := cart.NewCartServiceClient(p.cartConn)
	items, err := c.GetCart(userID)
	if err != nil {
		return nil, fmt.Errorf("fetch cart: %w", err)
	}
	if len(items) == 0 {
		return nil, ErrEmptyCart
	}
	if err := validateCartItems(items); err != nil {
		return nil, fmt.Errorf("invalid cart: %w", err)
	}
	return items, nil
}

// CalculatePricing computes subtotal, applies promo discount, and adds shipping.
func (p *CheckoutPipeline) CalculatePricing(userID, promoCode string, items []*cart.CartItem) PricingResult {
	subtotal := computeTotal(items)
	discount := p.applyPromoDiscount(userID, promoCode, subtotal)
	shipping := estimateShipping(items)
	return PricingResult{
		Subtotal: subtotal,
		Discount: discount,
		Shipping: shipping,
		Total:    subtotal - discount + shipping,
	}
}

// CommitOrder persists the order result and clears the user's cart.
func (p *CheckoutPipeline) CommitOrder(userID string, pr PricingResult) (*OrderResult, error) {
	c := cart.NewCartServiceClient(p.cartConn)
	orderID := generateOrderID()
	if err := c.ClearCart(userID); err != nil {
		return nil, fmt.Errorf("clear cart: %w", err)
	}
	result := &OrderResult{
		OrderID:  orderID,
		Total:    pr.Total,
		Discount: pr.Discount,
		Status:   "confirmed",
	}
	orders.save(result)
	return result, nil
}

// Execute runs the full checkout pipeline for a user with an optional promo code.
func (p *CheckoutPipeline) Execute(userID, promoCode string) (*OrderResult, error) {
	if err := p.ValidateRequest(userID); err != nil {
		return nil, err
	}
	items, err := p.FetchCart(userID)
	if err != nil {
		return nil, err
	}
	pr := p.CalculatePricing(userID, promoCode, items)
	return p.CommitOrder(userID, pr)
}

// applyPromoDiscount contacts the promo service to apply a discount code.
func (p *CheckoutPipeline) applyPromoDiscount(userID, code string, total float64) float64 {
	if code == "" || p.promoConn == nil {
		return 0
	}
	client := promo.NewPromoServiceClient(p.promoConn)
	discount, err := client.ApplyPromo(userID, code, total)
	if err != nil {
		return 0
	}
	return discount
}

// validateCartItems ensures no item has a zero price or non-positive quantity.
func validateCartItems(items []*cart.CartItem) error {
	for _, it := range items {
		if it.Price <= 0 {
			return fmt.Errorf("item %s has invalid price %.2f", it.ProductID, it.Price)
		}
		if it.Quantity <= 0 {
			return fmt.Errorf("item %s has invalid quantity %d", it.ProductID, it.Quantity)
		}
	}
	return nil
}

// estimateShipping returns a flat shipping fee based on item count.
func estimateShipping(items []*cart.CartItem) float64 {
	if len(items) == 0 {
		return 0
	}
	return 5.0 + float64(len(items))*0.5
}
