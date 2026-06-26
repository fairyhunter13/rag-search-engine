package main

import (
	"log"
	"net"
	"net/http"

	"google.golang.org/grpc"
	cart "example.com/shop/cart-svc/cart"
	promo "example.com/shop/promo-svc/promo"
	"example.com/shop/checkout-svc/checkout"
)

func main() {
	cartConn, err := grpc.Dial("cart-svc:50051", grpc.WithInsecure())
	if err != nil {
		log.Fatalf("dial cart: %v", err)
	}
	defer cartConn.Close()

	promoConn, err := grpc.Dial("promo-svc:50052", grpc.WithInsecure())
	if err != nil {
		log.Fatalf("dial promo: %v", err)
	}
	defer promoConn.Close()

	_ = cart.NewCartServiceClient(cartConn)
	_ = promo.NewPromoServiceClient(promoConn)

	svc := checkout.NewService(cartConn, promoConn)
	checkout.RegisterHTTPHandlers(svc)

	lis, err := net.Listen("tcp", ":50053")
	if err != nil {
		log.Fatalf("listen: %v", err)
	}
	s := grpc.NewServer()
	checkout.RegisterCheckoutServiceServer(s, svc)

	go func() {
		log.Println("checkout-svc HTTP on :8080")
		log.Fatal(http.ListenAndServe(":8080", nil))
	}()

	log.Println("checkout-svc gRPC on :50053")
	s.Serve(lis)
}
