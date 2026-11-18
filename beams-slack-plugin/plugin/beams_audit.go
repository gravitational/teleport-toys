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
	"strings"

	"github.com/gravitational/trace"

	"github.com/gravitational/teleport/api/utils/keys"
	"github.com/gravitational/teleport/integrations/lib/logger"
	"github.com/gravitational/teleport/lib/tlsca"
)

// Plugins cannot write to Teleport's audit log, and its LLM request events
// record models and token counts but not prompts. So the bot logs one
// structured line per request, which can be shipped alongside the audit log.
// Chat questions and answers also reach the audit log through
// recordChatInBeam.
// Chat lines carry the Teleport app session ID to match
// app.session.llm_request events.
const (
	requestLogMessage    = "Beams bot request"
	maxLoggedRequestText = 2000
)

// logRequest records what a Slack user asked the bot to do. attrs are extra
// key/value pairs, such as the beam or app session ID.
func (s beamsSession) logRequest(ctx context.Context, kind, text string, attrs ...any) {
	args := []any{
		"kind", kind,
		"slack_team_id", s.teamID,
		"slack_user_id", s.slackUserID,
		"teleport_user", s.username,
	}
	args = append(args, attrs...)
	if !s.runner.Config.OmitRequestText {
		if len(text) > maxLoggedRequestText {
			text = text[:maxLoggedRequestText] + "…"
		}
		args = append(args, "request", text)
	}
	logger.Get(ctx).InfoContext(ctx, requestLogMessage, args...)
}

// maxAuditedChatText keeps the recorded command under Linux's 128 KiB limit
// for a single argument; Slack messages and chat answers fit well within it.
const maxAuditedChatText = 32 << 10

// recordChatInBeam puts a chat question and answer into Teleport's audit log.
// Teleport audits non-interactive SSH commands in beams with their full
// command line and records their output, so the bot runs a harmless printf
// of both in one of the user's beams, as the user.
func (s beamsSession) recordChatInBeam(ctx context.Context, beam, agent, question, answer string) {
	clip := func(text string) string {
		if len(text) > maxAuditedChatText {
			return strings.ToValidUTF8(text[:maxAuditedChatText], "") + "…"
		}
		return text
	}
	command := shellQuoteJoin([]string{
		"printf", `%s\n`,
		"Teleport Beams Bot chat (" + agentDisplayName(agent) + ")",
		"Q: " + clip(question),
		"A: " + clip(answer),
	})
	if _, err := s.userTSH(ctx, "beams", "exec", beam, command); err != nil {
		logger.Get(ctx).WarnContext(ctx, "Failed to record chat in Teleport's audit log", "beam", beam, "error", err)
		return
	}
	logger.Get(ctx).InfoContext(ctx, "Recorded chat in Teleport's audit log", "beam", beam, "teleport_user", s.username)
}

// attachmentNames lists attachments for the request log.
func attachmentNames(files []attachment) []string {
	var names []string
	for _, f := range files {
		names = append(names, f.Name)
	}
	return names
}

// appSessionID reads the Teleport app session ID from an app certificate.
func appSessionID(certPEM []byte) (string, error) {
	cert, _, err := keys.X509Certificate(certPEM)
	if err != nil {
		return "", trace.Wrap(err)
	}
	identity, err := tlsca.FromSubject(cert.Subject, cert.NotAfter)
	if err != nil {
		return "", trace.Wrap(err)
	}
	return identity.RouteToApp.SessionID, nil
}
