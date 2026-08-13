// Package livekitauth mints LiveKit access tokens by hand -- a small HS256 JWT with a
// "video" grant claim -- instead of importing github.com/livekit/protocol/auth.
//
// Why hand-rolled, specifically the `auth` subpackage: measured it directly rather than
// assuming -- importing github.com/livekit/protocol/auth alone roughly DOUBLES this
// binary (27MB -> 54MB local measurement), while github.com/livekit/protocol/livekit
// (just the generated protobuf message types, used by internal/livekitadmin for
// RoomService calls) adds no measurable weight at all. So the module itself isn't the
// problem -- the auth package specifically pulls in enough of the surrounding protocol
// machinery to matter. The token format is a documented, stable JWT shape, cheap to
// implement directly with golang-jwt, which gin already pulls in transitively -- not
// worth 27MB of image size and cold-start build time (plan §8) to avoid ~40 lines of code.
package livekitauth

import (
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// VideoGrant mirrors LiveKit's documented JWT "video" claim shape.
// https://docs.livekit.io/home/get-started/authentication/
type VideoGrant struct {
	RoomJoin     bool   `json:"roomJoin,omitempty"`
	Room         string `json:"room,omitempty"`
	CanPublish   bool   `json:"canPublish"`
	CanSubscribe bool   `json:"canSubscribe"`
	RoomCreate   bool   `json:"roomCreate,omitempty"`
}

type claims struct {
	jwt.RegisteredClaims
	Video VideoGrant `json:"video"`
}

// MintToken returns a signed access token for `identity` to join `room`.
// TODO(workstream A): confirm the TTL (currently 1h) against how long a single call is
// allowed to run before the frontend needs to re-auth.
func MintToken(apiKey, apiSecret, room, identity string, ttl time.Duration) (string, error) {
	now := time.Now()
	c := claims{
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer:    apiKey,
			Subject:   identity,
			IssuedAt:  jwt.NewNumericDate(now),
			NotBefore: jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(now.Add(ttl)),
		},
		Video: VideoGrant{
			RoomJoin:     true,
			Room:         room,
			CanPublish:   true,
			CanSubscribe: true,
		},
	}

	token := jwt.NewWithClaims(jwt.SigningMethodHS256, c)
	return token.SignedString([]byte(apiSecret))
}

// MintAdminToken returns a short-lived server-to-server token for RoomService calls
// (internal/livekitadmin) -- roomCreate grant, no room/identity scoping, matching
// LiveKit's documented admin-token shape. Minted fresh per call rather than cached: JWT
// signing is a local, synchronous, negligible-cost operation, so there's no reason to
// manage a cached token's lifecycle for this.
func MintAdminToken(apiKey, apiSecret string, ttl time.Duration) (string, error) {
	now := time.Now()
	c := claims{
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer:    apiKey,
			IssuedAt:  jwt.NewNumericDate(now),
			NotBefore: jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(now.Add(ttl)),
		},
		Video: VideoGrant{RoomCreate: true},
	}
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, c)
	return token.SignedString([]byte(apiSecret))
}
