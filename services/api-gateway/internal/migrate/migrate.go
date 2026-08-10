// Package migrate runs infra/postgres/migrations/*.sql via golang-migrate at process
// startup, idempotently (applied migrations are tracked in the `schema_migrations` table
// golang-migrate manages itself). This replaces relying on Postgres's own
// docker-entrypoint-initdb.d, which only ever runs against a fresh/empty volume -- fine
// for the very first boot, silently wrong the moment a second migration file exists and
// someone's running against a volume from before it was added.
package migrate

import (
	"errors"
	"fmt"

	"github.com/golang-migrate/migrate/v4"
	_ "github.com/golang-migrate/migrate/v4/database/postgres" // driver registration side effect
	_ "github.com/golang-migrate/migrate/v4/source/file"       // source registration side effect
)

// Run applies every pending up migration found under sourcePath (a directory of
// 000N_*.up.sql / .down.sql pairs) against databaseURL. Safe to call on every process
// start: a no-op if the schema is already current.
func Run(sourcePath, databaseURL string) error {
	m, err := migrate.New(fmt.Sprintf("file://%s", sourcePath), databaseURL)
	if err != nil {
		return fmt.Errorf("migrate: init: %w", err)
	}
	defer m.Close()

	if err := m.Up(); err != nil && !errors.Is(err, migrate.ErrNoChange) {
		return fmt.Errorf("migrate: up: %w", err)
	}
	return nil
}
