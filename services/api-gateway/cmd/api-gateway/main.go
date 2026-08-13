// Command api-gateway is the control-plane REST API described in
// specs/implementation-plan.md §4.1 and docs/openapi/api-gateway.yaml.
package main

import (
	"context"
	"log/slog"
	"os"

	"techsphere2026/api-gateway/internal/config"
	"techsphere2026/api-gateway/internal/db"
	"techsphere2026/api-gateway/internal/httpapi"
	"techsphere2026/api-gateway/internal/livekitadmin"
	"techsphere2026/api-gateway/internal/migrate"
	"techsphere2026/api-gateway/internal/vectorstore"
)

func main() {
	ctx := context.Background()

	cfg, err := config.Load()
	if err != nil {
		slog.Error("config error", "error", err)
		os.Exit(1)
	}

	slog.Info("running migrations", "path", cfg.MigrationsPath)
	if err := migrate.Run(cfg.MigrationsPath, cfg.DatabaseURL); err != nil {
		slog.Error("migration failed", "error", err)
		os.Exit(1)
	}

	pool, err := db.Connect(ctx, cfg.DatabaseURL)
	if err != nil {
		slog.Error("database connection failed", "error", err)
		os.Exit(1)
	}
	defer pool.Close()

	server := &httpapi.Server{
		DB:          pool,
		VectorStore: vectorstore.New(cfg.VectorStoreURL),
		LiveKit:     livekitadmin.New(cfg.LiveKitHostURL, cfg.LiveKitAPIKey, cfg.LiveKitAPISecret),
		Cfg:         cfg,
	}

	router := httpapi.NewRouter(server)

	slog.Info("api-gateway listening", "port", cfg.Port, "llm_model", cfg.LLMModel, "stt_mode", cfg.STTMode)
	if err := router.Run(":" + cfg.Port); err != nil {
		slog.Error("server exited", "error", err)
		os.Exit(1)
	}
}
