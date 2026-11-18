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
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"log/slog"
	"math/big"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/gravitational/teleport/integrations/lib/logger"
	"github.com/gravitational/teleport/lib/tlsca"
)

// requestLogs returns the "Beams bot request" lines written to buf.
func requestLogs(t *testing.T, buf *bytes.Buffer) []map[string]any {
	var lines []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(buf.String()), "\n") {
		var entry map[string]any
		require.NoError(t, json.Unmarshal([]byte(line), &entry))
		if entry["msg"] == requestLogMessage {
			lines = append(lines, entry)
		}
	}
	return lines
}

func TestRequestLog(t *testing.T) {
	tshPath, _ := writeFakeTSH(t)
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:     tshPath,
			ProfilesDir: t.TempDir(),
			BotName:     "teleport-beams-bot",
			Users:       map[string]string{"UALICE": "alice@example.com"},
		},
		MintIdentity: fakeMint,
	}
	var buf bytes.Buffer
	ctx := logger.WithLogger(t.Context(), slog.New(slog.NewJSONHandler(&buf, nil)))

	_, err := runner.Run(ctx, "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)
	_, err = runner.Ask(ctx, "T1", "UALICE", "C1-1700000000.005000", "make a webpage about Seattle")
	require.NoError(t, err)
	_, err = runner.Run(ctx, "T1", "UALICE", "exec mine uname -a")
	require.NoError(t, err)

	logs := requestLogs(t, &buf)
	require.Len(t, logs, 2, "connect is not logged, so session IDs stay out of logs")
	beam := logs[0]
	require.Equal(t, "beam", beam["kind"])
	require.Equal(t, "T1", beam["slack_team_id"])
	require.Equal(t, "UALICE", beam["slack_user_id"])
	require.Equal(t, "alice@example.com", beam["teleport_user"])
	require.Equal(t, "mine", beam["beam"])
	require.Equal(t, agentClaude, beam["agent"])
	require.Equal(t, "make a webpage about Seattle", beam["request"])
	slash := logs[1]
	require.Equal(t, "slash", slash["kind"])
	require.Equal(t, "exec", slash["command"])
	require.Equal(t, "exec mine uname -a", slash["request"])

	// The text can be left out.
	buf.Reset()
	runner.Config.OmitRequestText = true
	_, err = runner.Run(ctx, "T1", "UALICE", "ls")
	require.NoError(t, err)
	logs = requestLogs(t, &buf)
	require.Len(t, logs, 1)
	require.NotContains(t, logs[0], "request")
	require.Equal(t, "ls", logs[0]["command"])
}

func TestAppSessionID(t *testing.T) {
	identity := tlsca.Identity{
		Username:   "alice@example.com",
		Groups:     []string{"beam-user"},
		Expires:    time.Now().Add(time.Hour),
		RouteToApp: tlsca.RouteToApp{SessionID: "app-session-1", Name: "anthropic", PublicAddr: "anthropic.example.teleport.sh"},
	}
	subject, err := identity.Subject()
	require.NoError(t, err)
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	require.NoError(t, err)
	template := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      subject,
		NotBefore:    time.Now().Add(-time.Minute),
		NotAfter:     time.Now().Add(time.Hour),
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	require.NoError(t, err)

	id, err := appSessionID(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))
	require.NoError(t, err)
	require.Equal(t, "app-session-1", id)
}
