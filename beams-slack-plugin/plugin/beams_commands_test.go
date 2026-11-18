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
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gravitational/trace"
	"github.com/stretchr/testify/require"

	"github.com/gravitational/teleport/api/types"
	"github.com/gravitational/teleport/api/types/userloginstate"
)

type fakeBeamsUsers map[string][]string

func (f fakeBeamsUsers) GetUserLoginStates(context.Context) ([]*userloginstate.UserLoginState, error) {
	return nil, trace.NotImplemented("not used")
}

func (f fakeBeamsUsers) ListUserLoginStates(context.Context, int, string) ([]*userloginstate.UserLoginState, string, error) {
	return nil, "", trace.NotImplemented("not used")
}

func (f fakeBeamsUsers) GetUserLoginState(context.Context, string) (*userloginstate.UserLoginState, error) {
	return nil, trace.NotFound("no login state")
}

func (f fakeBeamsUsers) GetUser(_ context.Context, name string, _ bool) (types.User, error) {
	roles, ok := f[name]
	if !ok {
		return nil, trace.NotFound("user %q not found", name)
	}
	user, err := types.NewUser(name)
	if err != nil {
		return nil, err
	}
	user.SetRoles(roles)
	return user, nil
}

func TestBeamsResolveUser(t *testing.T) {
	emails := map[string]string{
		"UALLOWED": "alice@example.com",
		"UNOROLE":  "bob@example.com",
		"UMISSING": "carol@example.com",
		"UNOEMAIL": "",
	}
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			RequiredRole: "beam-user",
			Users:        map[string]string{"UOVERRIDE": "dave@example.com"},
		},
		LookupEmail: func(_ context.Context, userID string) (string, error) {
			return emails[userID], nil
		},
		Users: fakeBeamsUsers{
			"alice@example.com": {"access", "beam-user"},
			"bob@example.com":   {"access"},
		},
	}

	for _, tc := range []struct {
		slackUserID string
		want        string
	}{
		{slackUserID: "UALLOWED", want: "alice@example.com"},
		{slackUserID: "UOVERRIDE", want: "dave@example.com"},
		{slackUserID: "UNOROLE"},
		{slackUserID: "UMISSING"},
		{slackUserID: "UNOEMAIL"},
	} {
		t.Run(tc.slackUserID, func(t *testing.T) {
			got, err := runner.resolveUser(t.Context(), tc.slackUserID)
			if tc.want == "" {
				require.True(t, trace.IsAccessDenied(err), "expected access denied, got %v", err)
				return
			}
			require.NoError(t, err)
			require.Equal(t, tc.want, got)
		})
	}
}

func TestBeamTargets(t *testing.T) {
	require.Equal(t, []string{"crisp-array"}, beamTargets([]string{"exec", "crisp-array", "make", "-j4"}))
	require.Equal(t, []string{"crisp-array"}, beamTargets([]string{"publish", "--tcp", "crisp-array"}))
	require.Equal(t, []string{"a", "b"}, beamTargets([]string{"scp", "a:/x", "b:/y"}))
	require.Empty(t, beamTargets([]string{"scp", "local", "other"}))
	require.Empty(t, beamTargets([]string{"rm"}))
}

func TestParseCommand(t *testing.T) {
	for _, tc := range []struct {
		text string
		want []string
	}{
		{text: "codex mine what's in this directory?", want: []string{"codex", "mine", "what's in this directory?"}},
		{text: "claude mine -c   don't stop", want: []string{"claude", "mine", "-c", "don't stop"}},
		{text: `claude mine --continue "fix it"`, want: []string{"claude", "mine", "--continue", "fix it"}},
		{text: "codex mine", want: []string{"codex", "mine"}},
		{text: "rm `curious-shield`", want: []string{"rm", "curious-shield"}},
		{text: "codex `mine` what's up", want: []string{"codex", "mine", "what's up"}},
		{text: `exec mine "cd /app && make"`, want: []string{"exec", "mine", "cd /app && make"}},
	} {
		got, err := parseCommand(tc.text)
		require.NoError(t, err, tc.text)
		require.Equal(t, tc.want, got, tc.text)
	}
	_, err := parseCommand("exec mine echo don't")
	require.ErrorContains(t, err, "wrap arguments that contain quotes or apostrophes in double quotes")
}

func TestCleanTSHError(t *testing.T) {
	stderr := "\x1b[31mERROR:\x1b[0m cannot relogin in non-interactive session\n\t\"beam\" \"x\" does not exist\n"
	require.Equal(t, `"beam" "x" does not exist`, cleanTSHError(stderr))
}

func TestCodeBlock(t *testing.T) {
	require.Equal(t, "ok", codeBlock("ok\n"))
	require.Equal(t, "```line one\nline two```", codeBlock("line one\nline two\n"))
}

func TestStripANSI(t *testing.T) {
	require.Equal(t, "ERROR: boom", stripANSI("\x1b[31mERROR:\x1b[0m boom"))
}

// writeFakeTSH installs a tsh stand-in that serves a fixed set of bot-owned
// beams and records every invocation.
func writeFakeTSH(t *testing.T) (tshPath, logPath string) {
	dir := t.TempDir()
	logPath = filepath.Join(dir, "calls")
	tshPath = filepath.Join(dir, "tsh")
	script := `#!/bin/sh
echo "$@" >> ` + logPath + `
case "$3 $4" in
"beams ls") echo '[{"id":"mine","owner":"access-plugin"},{"id":"theirs","owner":"access-plugin"}]' ;;
"beams add") echo '{"id":"mine","owner":"access-plugin"}' ;;
"beams exec") printf 'Built the page.\nBEAMS_BOT_ACTION: publish\n' ;;
"beams publish") echo '{"id":"mine","url":"https://mine.example"}' ;;
*) echo ok ;;
esac
`
	require.NoError(t, os.WriteFile(tshPath, []byte(script), 0o700))
	return tshPath, logPath
}

func TestBeamsRequireConnection(t *testing.T) {
	tshPath, logPath := writeFakeTSH(t)
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:        tshPath,
			Proxy:          "example.teleport.sh:443",
			PluginIdentity: "/plugin-identity",
			ProfilesDir:    t.TempDir(),
			BotName:        "teleport-beams-bot",
			DelegationTTL:  "168h",
			Users:          map[string]string{"UALICE": "alice@example.com"},
		},
		MintIdentity: fakeMint,
	}
	ctx := t.Context()

	// Every beam action asks for authorization first and runs nothing.
	for _, command := range []string{"ls", "add", "exec mine uname -a", "claude mine hi", "codex mine hi", "publish mine", "rm mine", "scp ./f mine:/tmp"} {
		out, err := runner.Run(ctx, "T1", "UALICE", command)
		require.NoError(t, err, command)
		require.Contains(t, out, "Before we can get started, you need to allow me to create beams as you.", command)
		require.Contains(t, out, "--user=alice@example.com --bot=teleport-beams-bot", command)
		require.Contains(t, out, "/beams connect <session-id>", command)
		require.False(t, strings.HasPrefix(out, "```"), "the prompt has its own code block and must not be wrapped again")
	}
	_, err := os.Stat(logPath)
	require.True(t, os.IsNotExist(err), "no tsh commands may run before the user connects")

	out, err := runner.Run(ctx, "T1", "UALICE", "status")
	require.NoError(t, err)
	require.Equal(t, "Not connected. Run `/beams connect`.", out)

	// After connecting, commands run as the user, never as the bot.
	_, err = runner.Run(ctx, "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)
	for _, command := range []string{"add", "ls", "exec mine uname -a", "claude mine --continue \"what's failing?\"", "codex mine what's failing?", "codex mine -c fix it", "rm mine"} {
		_, err := runner.Run(ctx, "T1", "UALICE", command)
		require.NoError(t, err, command)
	}
	// ls is a readable list, not raw JSON in a code block.
	out, err = runner.Run(ctx, "T1", "UALICE", "ls")
	require.NoError(t, err)
	require.True(t, strings.HasPrefix(out, "You have *2 beams*:\n• `mine`"), out)
	require.NotContains(t, out, "```")

	calls, err := os.ReadFile(logPath)
	require.NoError(t, err)
	require.NotContains(t, string(calls), "--identity=/plugin-identity")
	require.Contains(t, string(calls), "beams exec mine uname -a")
	require.Contains(t, string(calls), `beams exec mine 'claude' '-p' '--dangerously-skip-permissions' '--continue' 'what'\''s failing?'`)
	require.Contains(t, string(calls), `beams exec mine 'codex' 'exec' '--skip-git-repo-check' '--dangerously-bypass-approvals-and-sandbox' 'what'\''s failing?'`)
	require.Contains(t, string(calls), `beams exec mine 'codex' 'exec' '--skip-git-repo-check' '--dangerously-bypass-approvals-and-sandbox' 'resume' '--last' 'fix it'`)

	_, err = runner.Run(ctx, "T1", "UALICE", "codex mine")
	require.ErrorContains(t, err, "usage: /beams codex <name> [--continue] <prompt...>")

	// An expired session is forgotten and the user is asked again.
	profile := filepath.Join(runner.Config.ProfilesDir, "T1", "UALICE")
	require.NoError(t, os.WriteFile(filepath.Join(profile, "delegation-session"), []byte("99999999-2222-3333-4444-555555555555"), 0o600))
	out, err = runner.Run(ctx, "T1", "UALICE", "ls")
	require.NoError(t, err)
	require.Contains(t, out, "Your authorization has expired.\n*Before we can get started")
	out, err = runner.Run(ctx, "T1", "UALICE", "status")
	require.NoError(t, err)
	require.Equal(t, "Not connected. Run `/beams connect`.", out)
}

func TestBeamsRetryUntilReady(t *testing.T) {
	oldPoll := beamReadyPoll
	beamReadyPoll = 10 * time.Millisecond
	t.Cleanup(func() { beamReadyPoll = oldPoll })

	dir := t.TempDir()
	counter := filepath.Join(dir, "attempts")
	tshPath := filepath.Join(dir, "tsh")
	script := `#!/bin/sh
n=$(cat ` + counter + ` 2>/dev/null || echo 0)
n=$((n+1))
echo $n > ` + counter + `
if [ $n -le 2 ]; then
  echo 'ERROR: beam "fresh" is not ready to accept SSH connections' >&2
  exit 1
fi
echo ready
`
	require.NoError(t, os.WriteFile(tshPath, []byte(script), 0o700))

	s := beamsSession{
		runner:     BeamsCommandRunner{Config: BeamsConfig{TSHPath: tshPath}},
		profileDir: dir,
	}
	out, err := s.tsh(t.Context(), "/identity", "beams", "exec", "fresh", "true")
	require.NoError(t, err)
	require.Equal(t, "ready\n", out)
	attempts, err := os.ReadFile(counter)
	require.NoError(t, err)
	require.Equal(t, "3\n", string(attempts))
}

func TestFailedCommandReportsOutput(t *testing.T) {
	dir := t.TempDir()
	tshPath := filepath.Join(dir, "tsh")
	script := `#!/bin/sh
echo "API Error: 403 invalid session"
echo "ERROR: Process exited with status 1" >&2
exit 1
`
	require.NoError(t, os.WriteFile(tshPath, []byte(script), 0o700))
	s := beamsSession{runner: BeamsCommandRunner{Config: BeamsConfig{TSHPath: tshPath}}, profileDir: dir}
	_, _, err := s.tshOnce(t.Context(), "/identity", "beams", "exec", "mine", "claude -p hi")
	require.Error(t, err)
	require.Contains(t, trace.UserMessage(err), "API Error: 403 invalid session")
	require.Contains(t, trace.UserMessage(err), "Process exited with status 1")

	require.Equal(t, "…"+strings.Repeat("b", maxFailureOutput-1)+"c", outputTail(strings.Repeat("a", 50)+strings.Repeat("b", maxFailureOutput-1)+"c\n"))
}
