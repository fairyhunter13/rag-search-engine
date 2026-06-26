package main

import (
	"log"
	"net"

	"google.golang.org/grpc"
	"example.com/shop/cart-svc/cart"
)

func main() {
	lis, err := net.Listen("tcp", ":50051")
	if err != nil {
		log.Fatalf("listen: %v", err)
	}
	s := grpc.NewServer()
	cart.RegisterCartServiceServer(s, cart.NewCartServer())
	log.Println("cart-svc listening on :50051")
	if err := s.Serve(lis); err != nil {
		log.Fatalf("serve: %v", err)
	}
}
