// Package livekitadmin creates LiveKit rooms with metadata attached before anyone
// joins -- this is the piece that closes the "voice-agent has no way to know which
// patient/category a call is for" gap (specs/implementation-plan.md §2.1's
// _extract_call_context TODO). A plain join token (internal/livekitauth) is NOT enough
// for this: LiveKit auto-creates a room with no metadata on first join if it doesn't
// already exist, so setting metadata requires an explicit RoomService.CreateRoom call
// before minting the join token.
//
// Uses the official generated Twirp client (github.com/livekit/protocol/livekit) rather
// than hand-rolling the JSON request -- unlike auth.AccessToken (see
// internal/livekitauth's docstring for why that's avoided), importing just the generated
// message types + Twirp client added no measurable binary weight when checked directly.
// Using the real generated types also avoids having to guess whether LiveKit's Twirp
// JSON codec expects snake_case or camelCase field names -- protojson (which the
// generated client uses internally) is the same library LiveKit's own server uses, so
// this can't drift from the wire format.
package livekitadmin

import (
	"context"
	"net/http"
	"time"

	"github.com/livekit/protocol/livekit"
	"github.com/twitchtv/twirp"

	"techsphere2026/api-gateway/internal/livekitauth"
)

type Client struct {
	apiKey    string
	apiSecret string
	rpc       livekit.RoomService
}

func New(hostURL, apiKey, apiSecret string) *Client {
	return &Client{
		apiKey:    apiKey,
		apiSecret: apiSecret,
		rpc:       livekit.NewRoomServiceJSONClient(hostURL, &http.Client{Timeout: 10 * time.Second}),
	}
}

// CreateRoom creates a room with `metadataJSON` attached, idempotently -- calling this
// for a room name that already exists is a no-op success per LiveKit's own semantics,
// which is convenient: callers don't need to check existence first.
func (c *Client) CreateRoom(ctx context.Context, room, metadataJSON string) error {
	token, err := livekitauth.MintAdminToken(c.apiKey, c.apiSecret, time.Minute)
	if err != nil {
		return err
	}
	ctx, err = twirp.WithHTTPRequestHeaders(ctx, http.Header{"Authorization": []string{"Bearer " + token}})
	if err != nil {
		return err
	}
	_, err = c.rpc.CreateRoom(ctx, &livekit.CreateRoomRequest{
		Name:     room,
		Metadata: metadataJSON,
	})
	return err
}
