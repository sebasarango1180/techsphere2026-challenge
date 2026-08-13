package httpapi

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"

	"techsphere2026/api-gateway/internal/models"
)

// ListEscalations implements GET /api/v1/escalations?level=rojo -- the "alerts" view
// mentioned as a nice-to-have in specs/implementation-plan.md §1's frontend row.
func (s *Server) ListEscalations(c *gin.Context) {
	level := c.Query("level")

	var rows pgx.Rows
	var err error

	if level != "" {
		rows, err = s.DB.Query(c.Request.Context(), `
			SELECT id, call_id, level, rationale, triggered_by, cited_documents, created_at
			FROM escalations WHERE level = $1 ORDER BY created_at DESC
		`, level)
	} else {
		rows, err = s.DB.Query(c.Request.Context(), `
			SELECT id, call_id, level, rationale, triggered_by, cited_documents, created_at
			FROM escalations ORDER BY created_at DESC
		`)
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	defer rows.Close()

	out := []models.Escalation{}
	for rows.Next() {
		var e models.Escalation
		if err := rows.Scan(&e.ID, &e.CallID, &e.Level, &e.Rationale, &e.TriggeredBy, &e.CitedDocuments, &e.CreatedAt); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		out = append(out, e)
	}
	c.JSON(http.StatusOK, out)
}
