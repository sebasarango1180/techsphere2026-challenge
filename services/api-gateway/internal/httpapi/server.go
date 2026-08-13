// Package httpapi implements the routes documented in docs/openapi/api-gateway.yaml and
// specs/implementation-plan.md §4.1. One file per resource group, one Server holding the
// shared dependencies every handler needs.
package httpapi

import (
	"time"

	"github.com/gin-contrib/cors"
	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgxpool"

	"techsphere2026/api-gateway/internal/config"
	"techsphere2026/api-gateway/internal/livekitadmin"
	"techsphere2026/api-gateway/internal/vectorstore"
)

type Server struct {
	DB          *pgxpool.Pool
	VectorStore *vectorstore.Client
	LiveKit     *livekitadmin.Client
	Cfg         config.Config
}

func NewRouter(s *Server) *gin.Engine {
	r := gin.Default()

	// Both frontends (call-interface, admin-console) are separate SPAs calling this API
	// straight from the browser (plan §1) -- without this every request fails at the
	// CORS preflight before reaching a handler ("No 'Access-Control-Allow-Origin'
	// header", found live testing call-interface against a real running api-gateway).
	r.Use(cors.New(cors.Config{
		AllowOrigins:     s.Cfg.CORSOrigins,
		AllowMethods:     []string{"GET", "POST", "PUT", "DELETE", "OPTIONS"},
		AllowHeaders:     []string{"Origin", "Content-Type", "Authorization"},
		AllowCredentials: true,
		MaxAge:           12 * time.Hour,
	}))

	r.GET("/healthz", s.Healthz)
	r.GET("/docs", s.Docs)
	r.GET("/docs/openapi.yaml", s.OpenAPISpec)

	v1 := r.Group("/api/v1")
	{
		v1.GET("/documents", s.ListDocuments)
		v1.POST("/documents", s.UploadDocument)
		v1.PUT("/documents/:id", s.ReindexDocument)
		v1.GET("/documents/:id/status", s.DocumentStatus)
		v1.DELETE("/documents/:id", s.DeleteDocument)

		v1.POST("/patients", s.CreatePatient)
		v1.GET("/patients", s.ListPatients)
		v1.GET("/patients/:id", s.GetPatient)

		v1.POST("/calls", s.CreateCall)
		v1.GET("/calls", s.ListCalls)
		v1.GET("/calls/:id", s.GetCall)
		v1.GET("/calls/:id/summary", s.GetCallSummary)

		v1.GET("/escalations", s.ListEscalations)

		v1.GET("/metrics/summary", s.MetricsSummary)

		v1.POST("/internal/livekit/webhook", s.LiveKitWebhook)
	}

	return r
}

func (s *Server) Healthz(c *gin.Context) {
	c.JSON(200, gin.H{"status": "ok"})
}
