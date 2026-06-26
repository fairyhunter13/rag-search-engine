// Package federation is the top-level gateway for the shop federation.
// It wires together the cart, checkout, and promo services.
package federation

import (
	"context"
	"fmt"
	"net/http"
)

// ServiceConfig holds connection details for all federation members.
type ServiceConfig struct {
	CartAddr     string
	CheckoutAddr string
	PromoAddr    string
}

// Gateway routes external requests to the appropriate member service.
type Gateway struct {
	cfg ServiceConfig
}

// NewGateway creates a federation gateway with the given service config.
func NewGateway(cfg ServiceConfig) *Gateway {
	return &Gateway{cfg: cfg}
}

// HealthCheck pings all member services and returns any failures.
func (g *Gateway) HealthCheck(ctx context.Context) error {
	for name, addr := range map[string]string{
		"cart":     g.cfg.CartAddr,
		"checkout": g.cfg.CheckoutAddr,
		"promo":    g.cfg.PromoAddr,
	} {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, addr+"/healthz", nil)
		if err != nil {
			return fmt.Errorf("%s health request: %w", name, err)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return fmt.Errorf("%s unreachable: %w", name, err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return fmt.Errorf("%s returned %d", name, resp.StatusCode)
		}
	}
	return nil
}
