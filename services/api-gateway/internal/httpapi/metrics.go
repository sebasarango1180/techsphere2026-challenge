package httpapi

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"techsphere2026/api-gateway/internal/models"
)

// MetricsSummary implements GET /api/v1/metrics/summary -- this is what makes the
// README's required metrics table (specs/implementation-plan.md §0) a real query against
// turns.stt_ms/retrieval_ms/llm_ms/tts_ms instead of a hand-typed, un-auditable number.
// The rubric explicitly penalizes numbers that don't hold up against the logs, so this
// endpoint (or the SQL behind it) is what any reported number in the README must trace
// back to.
func (s *Server) MetricsSummary(c *gin.Context) {
	var out models.MetricsSummary

	// End-to-end turn latency = time from patient speech ending to agent audio starting,
	// approximated here as stt_ms + retrieval_ms + llm_ms + tts_ms on agent turns. This is
	// the plan §0-required "P50/P95 latency" metric.
	err := s.DB.QueryRow(c.Request.Context(), `
		SELECT
			COALESCE(percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms), 0),
			COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms), 0),
			COALESCE(SUM(tokens_in), 0),
			COALESCE(SUM(tokens_out), 0)
		FROM (
			SELECT
				COALESCE(stt_ms, 0) + COALESCE(retrieval_ms, 0) + COALESCE(llm_ms, 0) + COALESCE(tts_ms, 0) AS total_ms,
				tokens_in, tokens_out
			FROM turns
			WHERE role = 'agent'
		) t
	`).Scan(&out.P50Ms, &out.P95Ms, &out.TokensIn, &out.TokensOut)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	err = s.DB.QueryRow(c.Request.Context(), `
		SELECT COALESCE(
			(SELECT COUNT(*) FROM turns WHERE retrieval_ms IS NOT NULL)::float
			/ NULLIF((SELECT COUNT(DISTINCT id) FROM calls), 0),
		0)
	`).Scan(&out.RAGQueriesPerCall)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	// TODO(workstream A/F): estimated cost per call needs a documented pricing model --
	// the rubric requires "extrapolate to production API prices and explain the calc"
	// (specs/implementation-plan.md §0). This is a real decision (which provider's prices,
	// which token counts) that belongs in the README's metrics section, not invented here.
	// Wire it in once that methodology is decided; 0 is a placeholder, not an answer.
	out.EstCostPerCall = 0

	c.JSON(http.StatusOK, out)
}
