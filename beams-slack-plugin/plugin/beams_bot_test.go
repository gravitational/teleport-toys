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
)

func TestBotRequest(t *testing.T) {
	for _, tc := range []struct {
		name     string
		event    BeamsMessageEvent
		followUp bool
		want     string
	}{
		{name: "mention", event: BeamsMessageEvent{EventType: "app_mention", User: "U1", Text: "<@UBOT> create a beam"}, want: "create a beam"},
		{name: "dm", event: BeamsMessageEvent{EventType: "message", ChannelType: "im", User: "U1", Text: "list my beams"}, want: "list my beams"},
		{name: "channel prefix", event: BeamsMessageEvent{EventType: "message", ChannelType: "channel", User: "U1", Text: "Teleport Beams Bot, make a page"}, want: "make a page"},
		{name: "typed mention", event: BeamsMessageEvent{EventType: "message", ChannelType: "channel", User: "U1", Text: "@teleport beams bot list my beams"}, want: "list my beams"},
		{name: "channel chatter", event: BeamsMessageEvent{EventType: "message", ChannelType: "channel", User: "U1", Text: "lunch?"}},
		{name: "longer word", event: BeamsMessageEvent{EventType: "message", ChannelType: "channel", User: "U1", Text: "teleport beams bots are neat"}},
		{name: "bot message", event: BeamsMessageEvent{EventType: "message", ChannelType: "im", User: "U1", BotID: "B1", Text: "hi"}},
		{name: "edit", event: BeamsMessageEvent{EventType: "message", ChannelType: "im", User: "U1", Subtype: "message_changed", Text: "hi"}},
		{name: "image only in a thread", followUp: true, event: BeamsMessageEvent{EventType: "message", ChannelType: "channel", User: "U1", Subtype: "file_share", Files: []SlackFile{{Name: "dog.png"}}}, want: "Here's an attachment."},
		{name: "mention with an image", event: BeamsMessageEvent{EventType: "app_mention", User: "U1", Text: "<@UBOT> tell me about this image", Files: []SlackFile{{Name: "dog.png"}}}, want: "tell me about this image"},
		{name: "mentions someone", event: BeamsMessageEvent{EventType: "app_mention", User: "U1", Text: "<@UBOT> tell <@UANNA> the site is down"}, want: "tell <@UANNA> the site is down"},
		{name: "empty mention", event: BeamsMessageEvent{EventType: "app_mention", User: "U1", Text: "<@UBOT>"}},
		{name: "thread follow-up", followUp: true, event: BeamsMessageEvent{EventType: "message", ChannelType: "channel", User: "U1", ThreadTS: "1.2", Text: "how's that going?"}, want: "how's that going?"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := botRequest(tc.event, tc.followUp, "UBOT")
			require.Equal(t, tc.want != "", ok)
			require.Equal(t, tc.want, got)
		})
	}
}

func TestParseBotActions(t *testing.T) {
	reply, actions := parseBotActions("Done.\n  BEAMS_BOT_ACTION: publish\nSee it soon.\nBEAMS_BOT_ACTION:\n")
	require.Equal(t, "Done.\nSee it soon.", reply)
	require.Equal(t, []string{"publish"}, actions)
}

func TestChooseBeam(t *testing.T) {
	beams := []map[string]any{
		{"id": "old", "expires": "2026-10-02T01:00:00Z"},
		{"id": "new", "expires": "2026-10-02T09:00:00Z"},
	}
	name, continued := chooseBeam(beams, "old", "anything")
	require.Equal(t, "old", name)
	require.True(t, continued)

	name, continued = chooseBeam(beams, "gone", "do it in old please")
	require.Equal(t, "old", name)
	require.False(t, continued)

	name, _ = chooseBeam(beams, "", "make a page")
	require.Equal(t, "new", name)

	name, _ = chooseBeam(nil, "", "make a page")
	require.Empty(t, name)
}

const testSessionID = "11111111-2222-3333-4444-555555555555"

// fakeMint accepts only testSessionID, like a delegation session the user
// created for the bot.
func fakeMint(_ context.Context, sessionID, _ string, path string) error {
	if sessionID != testSessionID {
		return trace.AccessDenied("delegation session is not valid")
	}
	return os.WriteFile(path, []byte("delegated"), 0o600)
}

func TestBotAsk(t *testing.T) {
	tshPath, logPath := writeFakeTSH(t)
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:            tshPath,
			Proxy:              "example.teleport.sh:443",
			PluginIdentity:     "/plugin-identity",
			ProfilesDir:        t.TempDir(),
			BotName:            "teleport-beams-bot",
			DelegationTTL:      "168h",
			Users:              map[string]string{"UALICE": "alice@example.com"},
			ClaudeSkipApproval: true,
		},
		MintIdentity: fakeMint,
	}
	ctx := t.Context()
	thread := "C1-1700000000.000100"

	// Not connected: the instructions go to a direct message, and the channel
	// thread only gets a pointer to it.
	res, err := runner.Handle(ctx, "T1", "UALICE", thread, "make a webpage about Seattle and publish it")
	require.NoError(t, err)
	require.Contains(t, res.Reply, "I've sent you a direct message")
	require.NotContains(t, res.Reply, "tsh")
	require.Contains(t, res.DM, "Before we can get started, you need to allow me to create beams as you.")
	require.Contains(t, res.DM, "tsh delegation create-session --proxy=example.teleport.sh:443 --user=alice@example.com --bot=teleport-beams-bot --allow-all --session-ttl=168h")
	_, err = os.Stat(logPath)
	require.True(t, os.IsNotExist(err), "no tsh commands may run before the user connects")

	// A bad ID sent in the DM is rejected and the request stays pending.
	dm := "D1-1700000000.000150"
	_, err = runner.Handle(ctx, "T1", "UALICE", dm, "here you go 99999999-2222-3333-4444-555555555555")
	require.Error(t, err)

	// The right ID connects and hands the request back to its thread.
	res, err = runner.Handle(ctx, "T1", "UALICE", dm, "here you go "+testSessionID)
	require.NoError(t, err)
	require.Equal(t, "Thanks, you're connected as alice@example.com. I'll pick up your request where you asked it.", res.Reply)
	require.Equal(t, &PendingRequest{Channel: "C1", ThreadTS: "1700000000.000100", Text: "make a webpage about Seattle and publish it"}, res.Resume)

	out, err := runner.Ask(ctx, "T1", "UALICE", thread, res.Resume.Text)
	require.NoError(t, err)
	require.Contains(t, out, "Built the page.")
	require.Contains(t, out, "Published: https://mine.example")
	require.NotContains(t, out, "BEAMS_BOT_ACTION")
	require.NotContains(t, out, "beam:")

	_, err = runner.Ask(ctx, "T1", "UALICE", thread, "make the title bigger")
	require.NoError(t, err)

	calls, err := os.ReadFile(logPath)
	require.NoError(t, err)
	require.NotContains(t, string(calls), "--identity=/plugin-identity", "the plugin must never act as its own bot identity")
	require.Contains(t, string(calls), "Request: make a webpage about Seattle and publish it'", "the pending request runs after connecting")
	require.Contains(t, string(calls), "beams publish mine --format=json")
	require.Contains(t, string(calls), "'--continue' 'You are the Teleport Beams Bot")

	_, err = runner.Ask(ctx, "T1", "UALICE", "../escape", "hi")
	require.Error(t, err)
}

func TestBotExpiredAuthorization(t *testing.T) {
	tshPath, _ := writeFakeTSH(t)
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:       tshPath,
			ProfilesDir:   t.TempDir(),
			BotName:       "teleport-beams-bot",
			DelegationTTL: "168h",
			Users:         map[string]string{"UALICE": "alice@example.com"},
		},
		MintIdentity: fakeMint,
	}
	profile := filepath.Join(runner.Config.ProfilesDir, "T1", "UALICE")
	require.NoError(t, os.MkdirAll(profile, 0o700))
	sessionPath := filepath.Join(profile, "delegation-session")
	require.NoError(t, os.WriteFile(sessionPath, []byte("99999999-2222-3333-4444-555555555555"), 0o600))

	res, err := runner.Handle(t.Context(), "T1", "UALICE", "C1-1700000000.000300", "make a page")
	require.NoError(t, err)
	require.Contains(t, res.Reply, "I've sent you a direct message")
	require.Contains(t, res.DM, "Your authorization has expired.\n*Before we can get started")
	_, err = os.Stat(sessionPath)
	require.True(t, os.IsNotExist(err), "the expired session should be forgotten")
}

func TestBotCreateBeamSwitchesThread(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "calls")
	adds := filepath.Join(dir, "adds")
	tshPath := filepath.Join(dir, "tsh")
	script := `#!/bin/sh
echo "$@" >> ` + logPath + `
case "$3 $4" in
"beams ls")
  case $(cat ` + adds + ` 2>/dev/null || echo 0) in
  0) echo '[]' ;;
  1) echo '[{"id":"first","expires":"2026-10-02T01:00:00Z"}]' ;;
  *) echo '[{"id":"first","expires":"2026-10-02T01:00:00Z"},{"id":"second","expires":"2026-10-02T02:00:00Z"}]' ;;
  esac ;;
"beams add")
  n=$(cat ` + adds + ` 2>/dev/null || echo 0); n=$((n+1)); echo $n > ` + adds + `
  if [ $n -eq 1 ]; then echo '{"id":"first"}'; else echo '{"id":"second"}'; fi ;;
"beams exec")
  case "$*" in
  *"a new beam"*) printf 'Sure.\nBEAMS_BOT_ACTION: create_beam\n' ;;
  *) echo 'Done.' ;;
  esac ;;
esac
`
	require.NoError(t, os.WriteFile(tshPath, []byte(script), 0o700))
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:            tshPath,
			Proxy:              "example.teleport.sh:443",
			PluginIdentity:     "/plugin-identity",
			ProfilesDir:        t.TempDir(),
			BotName:            "teleport-beams-bot",
			Users:              map[string]string{"UALICE": "alice@example.com"},
			ClaudeSkipApproval: true,
		},
		MintIdentity: fakeMint,
	}
	ctx := t.Context()
	thread := "C1-1700000000.000200"
	_, err := runner.Run(ctx, "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)

	out, err := runner.Ask(ctx, "T1", "UALICE", thread, "make a page")
	require.NoError(t, err)
	require.NotContains(t, out, "beam:")
	require.Contains(t, out, "Created beam `first`.")

	out, err = runner.Ask(ctx, "T1", "UALICE", thread, "create a new beam")
	require.NoError(t, err)
	require.Contains(t, out, "Created beam `second`. This thread now works in it.")

	_, err = runner.Ask(ctx, "T1", "UALICE", thread, "hello")
	require.NoError(t, err)
	_, err = runner.Ask(ctx, "T1", "UALICE", thread, "again")
	require.NoError(t, err)

	var execs []string
	data, err := os.ReadFile(logPath)
	require.NoError(t, err)
	for _, line := range strings.Split(string(data), "\n") {
		if strings.Contains(line, "beams exec") {
			execs = append(execs, line)
		}
	}
	require.Len(t, execs, 4)
	require.Contains(t, execs[0], "beams exec first ")
	require.NotContains(t, execs[0], "'--continue'")
	require.Contains(t, execs[1], "beams exec first ")
	require.Contains(t, execs[1], "'--continue'")
	require.Contains(t, execs[2], "beams exec second ")
	require.NotContains(t, execs[2], "'--continue'", "first run in a newly created beam must start fresh")
	require.Contains(t, execs[3], "beams exec second ")
	require.Contains(t, execs[3], "'--continue'")
}

func TestIsBeamsListQuestion(t *testing.T) {
	for _, text := range []string{"how many beams do i have", "list my beams", "What beams do I have?", "show beams", "which beams are running"} {
		require.True(t, isBeamsListQuestion(text), text)
	}
	for _, text := range []string{"create a new beam", "what's in my beam", "make a webpage and publish it", "delete my beams", "build a site in one of my beams"} {
		require.False(t, isBeamsListQuestion(text), text)
	}
}

func TestFormatBeamsList(t *testing.T) {
	now := time.Date(2026, 10, 1, 22, 0, 0, 0, time.UTC)
	out := formatBeamsList([]map[string]any{
		{"id": "echoing-axis", "region": "us-west-2", "expires": "2026-10-02T21:00:00.123Z"},
		{"id": "turbo-path", "region": "us-west-2", "expires": "2026-10-01T22:30:00Z", "url": "https://turbo-path.example"},
	}, now, "none")
	require.Equal(t, "You have *2 beams*:\n• `echoing-axis` (us-west-2), expires in 23 hours\n• `turbo-path` (us-west-2), expires in 30 minutes, published at https://turbo-path.example", out)
	require.Equal(t, "none", formatBeamsList(nil, now, "none"))
}

func TestBotAnswersListFromTSH(t *testing.T) {
	tshPath, logPath := writeFakeTSH(t)
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:     tshPath,
			ProfilesDir: t.TempDir(),
			BotName:     "teleport-beams-bot",
			Users:       map[string]string{"UALICE": "alice@example.com"},
		},
		MintIdentity: fakeMint,
	}
	ctx := t.Context()
	_, err := runner.Run(ctx, "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)

	out, err := runner.Ask(ctx, "T1", "UALICE", "C1-1700000000.000400", "how many beams do i have")
	require.NoError(t, err)
	require.Contains(t, out, "You have *2 beams*:")
	require.Contains(t, out, "`mine`")
	require.True(t, runner.FollowsThread("T1", "UALICE", "C1-1700000000.000400"))

	calls, err := os.ReadFile(logPath)
	require.NoError(t, err)
	require.NotContains(t, string(calls), "beams exec", "list questions must not start Claude")
}

func TestBotBeamsIntro(t *testing.T) {
	for _, text := range []string{"what can i do with beams", "What can I do with Teleport Beams?", "what could we do with beams", "what are beams", "what is a beam?", "what are beams for"} {
		require.True(t, isBeamsIntroQuestion(text), text)
		require.True(t, isInstantRequest(text), text)
	}
	for _, text := range []string{"how many beams do i have", "what beams do i have", "what can i do with beams to build a website"} {
		require.False(t, isBeamsIntroQuestion(text), text)
	}

	runner := BeamsCommandRunner{Config: BeamsConfig{TSHPath: "/nonexistent/tsh", ProfilesDir: t.TempDir()}}
	out, err := runner.Ask(t.Context(), "T1", "UNOBODY", "C1-1700000000.000700", "what can I do with beams?")
	require.NoError(t, err)
	require.Equal(t, beamsIntro(false), out)
	require.NotContains(t, out, "live website")
	require.Contains(t, beamsIntro(true), "live website is down")
	require.Contains(t, out, "/beams help")
	require.Contains(t, out, "https://goteleport.com/docs/beams/")

	bot := &fakeBeamsBot{}
	app := &BeamsApp{bot: bot, runner: runner, seen: newRecentSet(10), threads: newThreadQueue()}
	app.handleMessage(t.Context(), BeamsMessageEvent{TeamID: "T1", User: "UNOBODY", Channel: "C1", TS: "1700000000.000800"}, "what are beams")
	require.Equal(t, []string{beamsIntro(false)}, bot.replies, "no \"On it\" message for instant answers")
}

func TestRequestedAgent(t *testing.T) {
	for text, want := range map[string]string{
		"use codex to make a webpage":           agentCodex,
		"make a webpage with Codex":             agentCodex,
		"codex: fix the tests":                  agentCodex,
		"switch back, use claude code":          agentClaude,
		"Claude, summarize the logs":            agentClaude,
		"make a webpage comparing claude codex": "",
		"make a webpage":                        "",
	} {
		require.Equal(t, want, requestedAgent(text), text)
	}
}

func TestThreadStateCompatibility(t *testing.T) {
	path := filepath.Join(t.TempDir(), "thread")
	require.NoError(t, os.WriteFile(path, []byte("mine new"), 0o600))
	require.Equal(t, threadState{beam: "mine", fresh: true, agent: agentClaude}, readThreadState(path))
	require.NoError(t, writeThreadState(path, threadState{beam: "mine", agent: agentCodex}))
	require.Equal(t, threadState{beam: "mine", agent: agentCodex}, readThreadState(path))
	require.Equal(t, threadState{agent: agentClaude}, readThreadState(filepath.Join(t.TempDir(), "missing")))
}

func TestBotAgentPerThread(t *testing.T) {
	tshPath, logPath := writeFakeTSH(t)
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:     tshPath,
			ProfilesDir: t.TempDir(),
			BotName:     "teleport-beams-bot",
			Users:       map[string]string{"UALICE": "alice@example.com"},
		},
		MintIdentity: fakeMint,
	}
	ctx := t.Context()
	_, err := runner.Run(ctx, "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)
	thread := "C1-1700000000.000900"

	out, err := runner.Ask(ctx, "T1", "UALICE", thread, "use codex to make a page")
	require.NoError(t, err)
	require.Contains(t, out, "Using Codex for this thread.")
	for _, text := range []string{"make the title bigger", "use claude to review it", "thanks, tweak the colors"} {
		_, err := runner.Ask(ctx, "T1", "UALICE", thread, text)
		require.NoError(t, err, text)
	}

	data, err := os.ReadFile(logPath)
	require.NoError(t, err)
	var execs []string
	for _, line := range strings.Split(string(data), "\n") {
		if strings.Contains(line, "beams exec") {
			execs = append(execs, line)
		}
	}
	require.Len(t, execs, 4)
	require.Contains(t, execs[0], "'codex' 'exec'")
	require.NotContains(t, execs[0], "'resume'")
	require.Contains(t, execs[1], "'codex' 'exec'")
	require.Contains(t, execs[1], "'resume' '--last'", "follow-ups continue the Codex conversation")
	require.Contains(t, execs[2], "'claude' '-p'")
	require.NotContains(t, execs[2], "'--continue'", "switching agents starts a fresh conversation")
	require.Contains(t, execs[3], "'claude' '-p'")
	require.Contains(t, execs[3], "'--continue'")
}

func TestRemoveTargets(t *testing.T) {
	beams := []map[string]any{{"id": "curious-shield"}, {"id": "mint-arc"}}
	for _, tc := range []struct {
		text   string
		thread string
		names  []string
		ok     bool
	}{
		{text: "delete curious-shield", names: []string{"curious-shield"}, ok: true},
		{text: "please remove beam `mint-arc`.", names: []string{"mint-arc"}, ok: true},
		{text: "delete curious-shield and mint-arc", names: []string{"curious-shield", "mint-arc"}, ok: true},
		{text: "delete this beam", thread: "mint-arc", names: []string{"mint-arc"}, ok: true},
		{text: "get rid of it", thread: "mint-arc", names: []string{"mint-arc"}, ok: true},
		{text: "delete my beams", ok: true},
		{text: "delete all my beams", names: []string{"curious-shield", "mint-arc"}, ok: true},
		{text: "please delete all of my beams.", names: []string{"curious-shield", "mint-arc"}, ok: true},
		{text: "delete all the files in mint-arc"},
		{text: "delete all"},
		{text: "delete this beam", ok: true},
		{text: "remove the background from the page in mint-arc"},
		{text: "make a page and delete the old one"},
		{text: "delete nonexistent-beam"},
	} {
		names, ok := removeTargets(tc.text, beams, tc.thread)
		require.Equal(t, tc.ok, ok, tc.text)
		require.Equal(t, tc.names, names, tc.text)
	}
}

func TestBotRemovesBeams(t *testing.T) {
	tshPath, logPath := writeFakeTSH(t)
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:     tshPath,
			ProfilesDir: t.TempDir(),
			BotName:     "teleport-beams-bot",
			Users:       map[string]string{"UALICE": "alice@example.com"},
		},
		MintIdentity: fakeMint,
	}
	ctx := t.Context()
	_, err := runner.Run(ctx, "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)

	out, err := runner.Ask(ctx, "T1", "UALICE", "C1-1700000000.001000", "delete `mine`")
	require.NoError(t, err)
	require.Equal(t, "Deleted beam `mine`.", out)

	out, err = runner.Ask(ctx, "T1", "UALICE", "C1-1700000000.001100", "delete my beams")
	require.NoError(t, err)
	require.Contains(t, out, "Which beam should I delete?")
	require.Contains(t, out, "`theirs`")

	_, err = runner.Ask(ctx, "T1", "UALICE", "C1-1700000000.001200", "remove the background from the page in mine")
	require.NoError(t, err)

	calls, err := os.ReadFile(logPath)
	require.NoError(t, err)
	require.Equal(t, 1, strings.Count(string(calls), "beams rm "), "only the explicit delete removes a beam")
	require.Contains(t, string(calls), "beams rm mine\n")
	require.Contains(t, string(calls), "beams exec mine", "the page edit goes to the agent")
}

// Ask returns just the thread reply, for tests that don't need DMs or resumes.
func (r BeamsCommandRunner) Ask(ctx context.Context, teamID, userID, threadKey, text string) (string, error) {
	res, err := r.Handle(ctx, teamID, userID, threadKey, text)
	return res.Reply, err
}

func TestBotAuthorizationInDirectMessage(t *testing.T) {
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
	ctx := t.Context()
	thread := "D1-1700000000.000100"

	// Asked in a DM: the instructions are the reply itself.
	res, err := runner.Handle(ctx, "T1", "UALICE", thread, "make a page")
	require.NoError(t, err)
	require.Contains(t, res.Reply, "Before we can get started")
	require.Contains(t, res.Reply, "--user=alice@example.com")
	require.Empty(t, res.DM)
	require.False(t, runner.Connected("T1", "UALICE"))

	// The session ID in the same thread connects and carries on right there.
	res, err = runner.Handle(ctx, "T1", "UALICE", thread, testSessionID)
	require.NoError(t, err)
	require.Nil(t, res.Resume)
	require.Contains(t, res.Reply, "Thanks, you're connected as alice@example.com.")
	require.Contains(t, res.Reply, "Built the page.")
	require.True(t, runner.Connected("T1", "UALICE"))
}

func TestBotAsksBeforeActing(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "calls")
	tshPath := filepath.Join(dir, "tsh")
	// Claude proposes a plan in plan mode and acts once allowed to.
	script := `#!/bin/sh
echo "$@" >> ` + logPath + `
case "$3 $4" in
"beams ls") echo '[{"id":"mine","owner":"alice@example.com"}]' ;;
"beams exec")
  case "$6" in
  *"'plan'"*) printf 'Plan:\n- write check.sh\n- run it\nBEAMS_BOT_ACTION: approve\nBEAMS_BOT_ACTION: publish\n' ;;
  *) printf 'Done: HTTP 200.\nBEAMS_BOT_ACTION: publish\n' ;;
  esac ;;
"beams publish") echo '{"id":"mine","url":"https://mine.example"}' ;;
*) echo ok ;;
esac
`
	require.NoError(t, os.WriteFile(tshPath, []byte(script), 0o700))
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:     tshPath,
			ProfilesDir: t.TempDir(),
			BotName:     "teleport-beams-bot",
			Users:       map[string]string{"UALICE": "alice@example.com"},
		},
		MintIdentity: fakeMint,
	}
	ctx := t.Context()
	_, err := runner.Run(ctx, "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)
	thread := "C1-1700000000.013000"

	// The plan comes back for approval, and nothing is published yet.
	out, err := runner.Ask(ctx, "T1", "UALICE", thread, "write a script to check example.com and run it")
	require.NoError(t, err)
	require.Equal(t, "Plan:\n- write check.sh\n- run it\n\nReply *yes* to go ahead, or tell me what to change.", out)
	calls, err := os.ReadFile(logPath)
	require.NoError(t, err)
	require.Contains(t, string(calls), `'claude' '-p' '--permission-mode' 'plan'`)
	require.NotContains(t, string(calls), "dangerously")
	require.NotContains(t, string(calls), "beams publish")

	// Anything but yes or no revises the plan, still in plan mode.
	out, err = runner.Ask(ctx, "T1", "UALICE", thread, "yes, but check example.org too")
	require.NoError(t, err)
	require.Contains(t, out, "Reply *yes*")
	calls, err = os.ReadFile(logPath)
	require.NoError(t, err)
	require.Contains(t, string(calls), `'--permission-mode' 'plan' '--continue' 'The user replied to your plan: yes, but check example.org too`)

	// Yes carries it out in the same conversation.
	out, err = runner.Ask(ctx, "T1", "UALICE", thread, "yes please")
	require.NoError(t, err)
	require.Contains(t, out, "Done: HTTP 200.")
	require.Contains(t, out, "Published: https://mine.example")
	calls, err = os.ReadFile(logPath)
	require.NoError(t, err)
	require.Contains(t, string(calls), `'claude' '-p' '--dangerously-skip-permissions' '--continue' 'The user approved your plan.`)

	// No drops a plan.
	_, err = runner.Ask(ctx, "T1", "UALICE", thread, "now delete the script")
	require.NoError(t, err)
	out, err = runner.Ask(ctx, "T1", "UALICE", thread, "no thanks")
	require.NoError(t, err)
	require.Equal(t, "OK, I won't go ahead with that.", out)
}
