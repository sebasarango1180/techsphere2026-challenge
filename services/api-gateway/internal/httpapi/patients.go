package httpapi

import (
	"encoding/json"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"

	"techsphere2026/api-gateway/internal/models"
)

// CreatePatient implements POST /api/v1/patients. This is the "admin sets category
// patient-wise" side of docs/dataset-eda.md §7's recommendation -- category/procedure
// are established here, before any call happens, never negotiated live during one.
func (s *Server) CreatePatient(c *gin.Context) {
	var req models.PatientCreate
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	comorbidities := req.Comorbidities
	if comorbidities == nil {
		comorbidities = []string{}
	}
	comorbiditiesJSON, err := json.Marshal(comorbidities)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	id := uuid.NewString()
	_, err = s.DB.Exec(c.Request.Context(), `
		INSERT INTO patients (id, external_ref, name, procedure, category, surgery_date,
		                       age, gender, comorbidities, national_id, address, city,
		                       department, eps)
		VALUES ($1, NULLIF($2,''), $3, NULLIF($4,''), $5, NULLIF($6,'')::date, $7,
		        NULLIF($8,''), $9::jsonb, NULLIF($10,''), NULLIF($11,''), NULLIF($12,''),
		        NULLIF($13,''), NULLIF($14,''))
	`, id, req.ExternalRef, req.Name, req.Procedure, req.Category, req.SurgeryDate,
		req.Age, req.Gender, string(comorbiditiesJSON), req.NationalID, req.Address,
		req.City, req.Department, req.EPS)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusCreated, gin.H{"id": id})
}

// ListPatients implements GET /api/v1/patients.
func (s *Server) ListPatients(c *gin.Context) {
	rows, err := s.DB.Query(c.Request.Context(), `
		SELECT id, external_ref, name, procedure, category, surgery_date, age, gender,
		       comorbidities, national_id, address, city, department, eps, created_at
		FROM patients
		ORDER BY created_at DESC
	`)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	defer rows.Close()

	out := []models.Patient{}
	for rows.Next() {
		p, err := scanPatient(rows)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		out = append(out, p)
	}
	c.JSON(http.StatusOK, out)
}

// GetPatient implements GET /api/v1/patients/:id.
func (s *Server) GetPatient(c *gin.Context) {
	id := c.Param("id")
	row := s.DB.QueryRow(c.Request.Context(), `
		SELECT id, external_ref, name, procedure, category, surgery_date, age, gender,
		       comorbidities, national_id, address, city, department, eps, created_at
		FROM patients WHERE id = $1
	`, id)
	p, err := scanPatient(row)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "patient not found"})
		return
	}
	c.JSON(http.StatusOK, p)
}

type rowScanner interface {
	Scan(dest ...any) error
}

func scanPatient(row rowScanner) (models.Patient, error) {
	var p models.Patient
	var surgeryDate *time.Time // pgx scans `date` columns into time.Time, not string
	var comorbiditiesJSON []byte
	err := row.Scan(
		&p.ID, &p.ExternalRef, &p.Name, &p.Procedure, &p.Category, &surgeryDate,
		&p.Age, &p.Gender, &comorbiditiesJSON, &p.NationalID, &p.Address, &p.City,
		&p.Department, &p.EPS, &p.CreatedAt,
	)
	if err != nil {
		return p, err
	}
	if surgeryDate != nil {
		formatted := surgeryDate.Format("2006-01-02")
		p.SurgeryDate = &formatted
	}
	p.Comorbidities = []string{}
	if len(comorbiditiesJSON) > 0 {
		if err := json.Unmarshal(comorbiditiesJSON, &p.Comorbidities); err != nil {
			return p, err
		}
	}
	return p, nil
}
