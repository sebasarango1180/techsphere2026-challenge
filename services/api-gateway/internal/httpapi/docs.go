package httpapi

import (
	"net/http"
	"os"

	"github.com/gin-gonic/gin"
)

// docsPageHTML renders the spec served at /docs/openapi.yaml via Redoc (loaded from its
// CDN -- this is a documentation nicety viewed after the timed setup, not part of it, so
// an external script tag here doesn't affect the G2 boot budget).
const docsPageHTML = `<!doctype html>
<html>
  <head>
    <title>api-gateway API docs</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
  </head>
  <body>
    <redoc spec-url="/docs/openapi.yaml"></redoc>
    <script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"></script>
  </body>
</html>`

func (s *Server) Docs(c *gin.Context) {
	c.Data(http.StatusOK, "text/html; charset=utf-8", []byte(docsPageHTML))
}

func (s *Server) OpenAPISpec(c *gin.Context) {
	spec, err := os.ReadFile(s.Cfg.OpenAPIPath)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "openapi spec not found at " + s.Cfg.OpenAPIPath})
		return
	}
	c.Data(http.StatusOK, "application/yaml; charset=utf-8", spec)
}
