package main

import (
	"log"
	"net"

	"google.golang.org/grpc"
	"example.com/shop/promo-svc/promo"
)

func main() {
	lis, err := net.Listen("tcp", ":50052")
	if err != nil {
		log.Fatalf("listen: %v", err)
	}
	s := grpc.NewServer()
	promo.RegisterPromoServiceServer(s, promo.NewPromoServer())
	log.Println("promo-svc listening on :50052")
	if err := s.Serve(lis); err != nil {
		log.Fatalf("serve: %v", err)
	}
}
