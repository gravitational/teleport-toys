/*
 * Teleport
 * Copyright (C) 2026  Gravitational, Inc.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

package slack

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// TestParseBeamsEvents checks that the Socket Mode client acks and forwards
// the envelope types the Beams app needs.
func TestParseBeamsEvents(t *testing.T) {
	tests := map[string]struct {
		payload string
		check   func(t *testing.T, event InteractionEvent)
	}{
		"slash command": {
			payload: `{"type":"slash_commands","envelope_id":"env-1","payload":{"command":"/beams","text":"ls","user_id":"U1","team_id":"T1","channel_id":"C1","response_url":"https://hooks.slack.com/x"}}`,
			check: func(t *testing.T, event InteractionEvent) {
				cmd, ok := event.(SlashCommandEvent)
				require.True(t, ok, "got %T", event)
				require.Equal(t, "/beams", cmd.Command)
				require.Equal(t, "ls", cmd.Text)
			},
		},
		"app mention": {
			payload: `{"type":"events_api","envelope_id":"env-1","payload":{"team_id":"T1","event":{"type":"app_mention","user":"U1","text":"<@UBOT> hi","channel":"C1","ts":"1.2"}}}`,
			check: func(t *testing.T, event InteractionEvent) {
				msg, ok := event.(BeamsMessageEvent)
				require.True(t, ok, "got %T", event)
				require.Equal(t, "app_mention", msg.EventType)
				require.Equal(t, "T1", msg.TeamID)
				require.Equal(t, "<@UBOT> hi", msg.Text)
			},
		},
	}
	for name, tt := range tests {
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			client := &SocketModeClient{interactionsCh: make(chan InteractionEvent, 1)}
			rawCh := make(chan json.RawMessage, 1)
			ackCh := make(chan string, 1)
			go client.parseEvents(ctx, rawCh, ackCh)

			rawCh <- json.RawMessage(tt.payload)
			select {
			case id := <-ackCh:
				require.Equal(t, "env-1", id)
			case <-time.After(5 * time.Second):
				t.Fatal("no ack")
			}
			select {
			case event := <-client.Interactions():
				tt.check(t, event)
			case <-time.After(5 * time.Second):
				t.Fatal("no event")
			}
		})
	}
}
