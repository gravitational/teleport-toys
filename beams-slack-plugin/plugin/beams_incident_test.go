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
	"regexp"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// writeIncidentTSH serves one app on webserver, where nginx is stopped until
// started.
func writeIncidentTSH(t *testing.T) (tshPath, logPath string) {
	dir := t.TempDir()
	logPath = filepath.Join(dir, "calls")
	tshPath = filepath.Join(dir, "tsh")
	started := filepath.Join(dir, "started")
	script := `#!/bin/sh
echo "$@" >> ` + logPath + `
case "$3 $4" in
"beams ls") echo '[{"id":"mine","owner":"alice@example.com"}]' ;;
"beams exec")
  case "$6" in
  *"'tsh' 'apps' 'ls'"*) echo '[{"metadata":{"name":"anthropic","labels":{}},"spec":{"uri":"llm://"}},{"metadata":{"name":"livewebsite","labels":{"hosted-on":"webserver"}},"spec":{"uri":"http://127.0.0.1"}}]' ;;
  *"'tsh' 'ssh' 'root@webserver' 'systemctl start"*) touch ` + started + ` ;;
  *"'tsh' 'ssh' 'root@webserver'"*"is-active"*) if [ -f ` + started + ` ]; then echo active; else echo inactive; fi ;;
  *"'tsh' 'ssh' 'root@webserver'"*"curl"*) if [ -f ` + started + ` ]; then printf 200; else printf 000; fi ;;
  esac ;;
*) echo ok ;;
esac
`
	require.NoError(t, os.WriteFile(tshPath, []byte(script), 0o700))
	return tshPath, logPath
}

func TestIncidentResponse(t *testing.T) {
	tshPath, logPath := writeIncidentTSH(t)
	slack := &fakeSlack{}
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:          tshPath,
			ProfilesDir:      t.TempDir(),
			BotName:          "teleport-beams-bot",
			Users:            map[string]string{"UALICE": "alice@example.com"},
			IncidentResponse: true,
			ServiceOwner:     "Alex Rivera",
		},
		MintIdentity: fakeMint,
		FindPeople: func(_ context.Context, query string) ([]SlackPerson, error) {
			return matchPeople([]SlackPerson{{ID: "UALEX", Name: "alex", RealName: "Alex Rivera"}, {ID: "UALEX2", Name: "alexk", RealName: "Alex King"}}, query), nil
		},
		Post: slack.post,
	}
	ctx := t.Context()
	_, err := runner.Run(ctx, "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)
	thread := "C1-1700000000.012000"

	// "live website is down" alone runs the whole incident response.
	out, err := runner.Ask(ctx, "T1", "UALICE", thread, "live website is down")
	require.NoError(t, err)
	require.Regexp(t, `^I contacted the service owner on your behalf\.\n\nI'm tracking this as incident #(\d{5})\.\n\n`, out)
	incident := regexp.MustCompile(`incident #(\d{5})`).FindStringSubmatch(out)[1]
	require.Contains(t, out, "I contacted the service owner on your behalf.")
	require.Equal(t, []string{"UALEX  There's an issue with the live website."}, slack.posts)
	require.Contains(t, out, "Found the `livewebsite` app in Teleport, labeled `hosted-on: webserver`")
	require.Contains(t, out, "From your beam `mine`, I connected to `webserver` through Teleport as `root` and checked nginx: it's *inactive*")
	require.Contains(t, out, "Reply *yes*")
	require.True(t, runner.FollowsThread("T1", "UALICE", thread))

	out, err = runner.Ask(ctx, "T1", "UALICE", thread, "yes please")
	require.NoError(t, err)
	require.Contains(t, out, "Started nginx on `webserver`, and it's *active*")
	require.Contains(t, out, "HTTP 200")
	require.Contains(t, out, "Let the service owner know it's resolved, and why it was down.")
	require.NotContains(t, out, "Incident #"+incident)
	require.Len(t, slack.posts, 2)
	require.Equal(t, "UALEX  The issue was resolved. The live website was down because nginx had stopped on `webserver` (it was *inactive*). I started nginx, and the site answers again.", slack.posts[1])
	calls, err := os.ReadFile(logPath)
	require.NoError(t, err)
	require.Contains(t, string(calls), `beams exec mine 'tsh' 'ssh' 'root@webserver' 'systemctl start nginx'`)
	require.NotRegexp(t, `--identity=\S+ (ssh|apps) `, string(calls), "tsh ssh and apps ls must run inside the beam, not from the plugin")

	// Checking again finds it running and offers nothing.
	out, err = runner.Ask(ctx, "T1", "UALICE", thread, "is the live website still down?")
	require.NoError(t, err)
	require.Contains(t, out, "it's *active*")
	require.NotContains(t, out, "Reply *yes*")

	// Off by default.
	runner.Config.IncidentResponse = false
	_, ok, err := beamsSession{runner: runner}.runIncident(ctx, filepath.Join(t.TempDir(), "x"), "live website is down", nil, "")
	require.NoError(t, err)
	require.False(t, ok)
}

func TestIncidentNotifyTarget(t *testing.T) {
	for text, want := range map[string]struct {
		target    string
		owner, ok bool
	}{
		"livewebsite is down, tell Alex that site is down, fix it please": {target: "Alex", ok: true},
		"notify the service owner there is an issue with their website":   {owner: true, ok: true},
		"send a DM to Alex Rivera about the live website":                 {target: "Alex Rivera", ok: true},
		"let Alex Rivera know the live website is down":                   {target: "Alex Rivera", ok: true},
		"tell <@UALEX> the live website is down":                          {target: "<@UALEX>", ok: true},
		"tell them the live website is down":                              {owner: true, ok: true},
		"tell me if the live website is down":                             {},
		"the live website is down":                                        {},
	} {
		target, owner, ok := incidentNotifyTarget(text)
		require.Equal(t, want.target, target, text)
		require.Equal(t, want.owner, owner, text)
		require.Equal(t, want.ok, ok, text)
	}
}

func TestIncidentTellsTheNamedPerson(t *testing.T) {
	tshPath, _ := writeIncidentTSH(t)
	slack := &fakeSlack{}
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:          tshPath,
			ProfilesDir:      t.TempDir(),
			BotName:          "teleport-beams-bot",
			Users:            map[string]string{"UALICE": "alice@example.com"},
			IncidentResponse: true,
			ServiceOwner:     "Alex Rivera",
		},
		MintIdentity: fakeMint,
		FindPeople: func(_ context.Context, query string) ([]SlackPerson, error) {
			return matchPeople([]SlackPerson{{ID: "UALEX", Name: "alex", RealName: "Alex Rivera"}, {ID: "UALEX2", Name: "alexk", RealName: "Alex King"}}, query), nil
		},
		Post: slack.post,
	}
	ctx := t.Context()
	_, err := runner.Run(ctx, "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)
	thread := "C1-1700000000.014000"

	// "Alex" matches two people; the service owner is one of them.
	out, err := runner.Ask(ctx, "T1", "UALICE", thread, "livewebsite is down, tell Alex that site is down, fix it please")
	require.NoError(t, err)
	require.Contains(t, out, "incident #")
	require.Contains(t, out, "I contacted the service owner on your behalf.")
	require.Equal(t, []string{"UALEX  There's an issue with the live website."}, slack.posts)

	_, err = runner.Ask(ctx, "T1", "UALICE", thread, "yes")
	require.NoError(t, err)
	require.Len(t, slack.posts, 2)
	require.True(t, strings.HasPrefix(slack.posts[1], "UALEX  The issue was resolved."))
}
