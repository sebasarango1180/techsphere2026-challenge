// Package config loads api-gateway's environment into a typed struct. Every field here
// mirrors a variable in the repo root .env.example -- keep the two in sync when adding
// new configuration rather than reading os.Getenv ad hoc elsewhere in the codebase.
package config

import (
	"fmt"
	"os"
	"strings"
)

type Config struct {
	Port             string
	DatabaseURL      string
	MigrationsPath   string
	OpenAPIPath      string
	VectorStoreURL   string
	LiveKitHostURL   string
	LiveKitAPIKey    string
	LiveKitAPISecret string
	// STTMode/LLMModel are recorded on each `calls` row for audit/metrics purposes --
	// api-gateway doesn't run STT or the LLM itself (voice-agent does), it just needs to
	// know what to log. Keep in sync with the same-named vars voice-agent reads.
	STTMode  string
	LLMModel string
	// CORSOrigins: both frontend apps (call-interface, admin-console) call this API
	// directly from the browser (plan §1 -- two separate SPAs, not one app with two
	// routes), so without this every request 404s at the browser's CORS preflight
	// before it ever reaches a handler -- found live, not anticipated: "No
	// 'Access-Control-Allow-Origin' header" blocking call-interface's real requests.
	// Comma-separated, defaults to both frontends' Vite dev ports.
	CORSOrigins []string
}

func Load() (Config, error) {
	cfg := Config{
		Port:             getenv("PORT", "8080"),
		DatabaseURL:      os.Getenv("DATABASE_URL"),
		// /app/migrations is where the Dockerfile copies infra/postgres/migrations to.
		// Local (non-Docker) runs need to override this -- see README.md.
		MigrationsPath:   getenv("MIGRATIONS_PATH", "/app/migrations"),
		// /app/openapi/api-gateway.yaml is where the Dockerfile bakes docs/openapi/api-gateway.yaml
		// in (same pattern as MigrationsPath above); local (non-Docker) runs need this pointed
		// at the real path instead, e.g. OPENAPI_PATH=../../docs/openapi/api-gateway.yaml.
		OpenAPIPath:      getenv("OPENAPI_PATH", "/app/openapi/api-gateway.yaml"),
		VectorStoreURL:   getenv("VECTOR_STORE_URL", "http://vector-store:8001"),
		LiveKitHostURL:   getenv("LIVEKIT_HOST_URL", "http://livekit:7880"),
		LiveKitAPIKey:    os.Getenv("LIVEKIT_API_KEY"),
		LiveKitAPISecret: os.Getenv("LIVEKIT_API_SECRET"),
		STTMode:          getenv("STT_MODE", "groq"),
		LLMModel:         getenv("OLLAMA_MODEL", "phi3.5:3.8b"),
		CORSOrigins:      splitCSV(getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:5174")),
	}

	if cfg.DatabaseURL == "" {
		return cfg, fmt.Errorf("DATABASE_URL is required")
	}
	if cfg.LiveKitAPIKey == "" || cfg.LiveKitAPISecret == "" {
		return cfg, fmt.Errorf("LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required")
	}

	return cfg, nil
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func splitCSV(v string) []string {
	parts := strings.Split(v, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}
