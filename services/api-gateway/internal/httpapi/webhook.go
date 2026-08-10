package httpapi

import (
	"io"
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"
)

// LiveKitWebhook implements POST /api/v1/internal/livekit/webhook.
//
// TODO(workstream A/E): LiveKit signs webhook payloads (Authorize header, JWT signed with
// the same API key/secret) -- verify that signature before trusting the body. Right now
// this just logs the raw payload so the endpoint exists and is wireable into LiveKit's
// server config; parsing room_started/room_finished events into `calls.started_at` /
// `calls.ended_at` updates is the next step. See
// https://docs.livekit.io/home/server/webhooks/ for the payload shape.
func (s *Server) LiveKitWebhook(c *gin.Context) {
	body, err := io.ReadAll(c.Request.Body)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	slog.Info("livekit webhook received", "body", string(body))
	c.Status(http.StatusOK)
}
