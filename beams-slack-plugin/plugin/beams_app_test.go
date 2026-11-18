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
	"sync"
	"testing"
	"time"

	"github.com/gravitational/trace"
	"github.com/stretchr/testify/require"
)

type fakeBeamsBot struct {
	mu      sync.Mutex
	replies []string
	dms     []string
}

func (b *fakeBeamsBot) GenerateWebSocketURL(context.Context) (string, error) { return "", nil }
func (b *fakeBeamsBot) TeleportClusterName() string                          { return "example.teleport.sh" }
func (b *fakeBeamsBot) PostBeamsReply(context.Context, string, string) error { return nil }
func (b *fakeBeamsBot) LookupEmailByUserID(context.Context, string) (string, error) {
	return "", nil
}

func (b *fakeBeamsBot) BotUserID(context.Context) (string, error) { return "UBOT", nil }
func (b *fakeBeamsBot) FindSlackPeople(context.Context, string) ([]SlackPerson, error) {
	return nil, nil
}

func (b *fakeBeamsBot) PostBeamsThreadReply(_ context.Context, channel, threadTS, text string) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if threadTS == "" {
		b.dms = append(b.dms, channel+": "+text)
		return nil
	}
	b.replies = append(b.replies, text)
	return nil
}

// blockingRunner holds the request "first" until release is closed.
type blockingRunner struct {
	mu      sync.Mutex
	asks    []string
	started chan string
	release chan struct{}
}

func (r *blockingRunner) Run(context.Context, string, string, string) (string, error) {
	return "", nil
}
func (r *blockingRunner) FollowsThread(string, string, string) bool { return true }
func (r *blockingRunner) Connected(string, string) bool             { return true }

func (r *blockingRunner) Handle(_ context.Context, _, _, _, text string) (AskResult, error) {
	r.mu.Lock()
	r.asks = append(r.asks, text)
	r.mu.Unlock()
	r.started <- text
	if text == "first" {
		<-r.release
	}
	return AskResult{Reply: "answer to " + text}, nil
}

func TestBotOneRequestPerThread(t *testing.T) {
	bot := &fakeBeamsBot{}
	runner := &blockingRunner{started: make(chan string, 10), release: make(chan struct{})}
	app := &BeamsApp{bot: bot, runner: runner, seen: newRecentSet(10), threads: newThreadQueue()}
	ctx := t.Context()
	event := BeamsMessageEvent{TeamID: "T1", User: "U1", Channel: "C1", TS: "1.1"}

	done := make(chan struct{})
	go func() {
		app.handleMessage(ctx, event, "first")
		close(done)
	}()
	require.Equal(t, "first", <-runner.started)

	// Messages that arrive mid-run are acknowledged and only the newest runs.
	followUp := event
	followUp.ThreadTS, followUp.TS = "1.1", "1.2"
	app.handleMessage(ctx, followUp, "hows it going?")
	followUp.TS = "1.3"
	app.handleMessage(ctx, followUp, "is it ready yet?")

	close(runner.release)
	<-done

	require.Equal(t, []string{"first", "is it ready yet?"}, runner.asks)
	require.Equal(t, []string{
		"On it. Working in your beam; this can take a few minutes.",
		botBusyReply,
		botBusyReply,
		"answer to first",
		"answer to is it ready yet?",
	}, bot.replies)

	// The thread is idle again, so the next message starts normally.
	followUp.TS = "1.4"
	app.handleMessage(ctx, followUp, "thanks")
	require.Equal(t, "thanks", runner.asks[len(runner.asks)-1])
	require.Equal(t, "On it. Working in your beam; this can take a few minutes.", bot.replies[len(bot.replies)-2])

	// Other threads are not blocked by a busy one.
	q := newThreadQueue()
	require.True(t, q.start("C1-1.1", queuedMessage{}))
	require.True(t, q.start("C1-2.2", queuedMessage{}))
	require.False(t, q.start("C1-1.1", queuedMessage{request: "later"}))
}

// authRunner scripts the authorization flow: a channel request before
// connecting, then the session ID in a DM, then the resumed request.
type authRunner struct {
	mu        sync.Mutex
	connected bool
	handled   []string
	resumed   chan struct{}
}

func (r *authRunner) Run(context.Context, string, string, string) (string, error) { return "", nil }
func (r *authRunner) FollowsThread(string, string, string) bool                   { return false }
func (r *authRunner) Connected(string, string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.connected
}

func (r *authRunner) Handle(_ context.Context, _, _, threadKey, text string) (AskResult, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.handled = append(r.handled, threadKey+" "+text)
	switch {
	case !r.connected && strings.HasPrefix(threadKey, "C1-"):
		return AskResult{Reply: "see your DMs", DM: "instructions"}, nil
	case !r.connected:
		r.connected = true
		return AskResult{Reply: "connected", Resume: &PendingRequest{Channel: "C1", ThreadTS: "1.1", Text: "make a page"}}, nil
	default:
		close(r.resumed)
		return AskResult{Reply: "page done"}, nil
	}
}

func TestBotAuthorizationFlowUsesDirectMessage(t *testing.T) {
	bot := &fakeBeamsBot{}
	runner := &authRunner{resumed: make(chan struct{})}
	app := &BeamsApp{bot: bot, runner: runner, seen: newRecentSet(10), threads: newThreadQueue()}
	ctx := t.Context()

	app.handleMessage(ctx, BeamsMessageEvent{TeamID: "T1", User: "UALICE", Channel: "C1", TS: "1.1"}, "make a page")
	require.Equal(t, []string{"see your DMs"}, bot.replies, "no \"On it\" before the user is connected")
	require.Equal(t, []string{"UALICE: instructions"}, bot.dms)

	app.handleMessage(ctx, BeamsMessageEvent{TeamID: "T1", User: "UALICE", Channel: "D1", TS: "2.2"}, "the-session-id")
	<-runner.resumed

	require.Eventually(t, func() bool {
		bot.mu.Lock()
		defer bot.mu.Unlock()
		return len(bot.replies) == 4
	}, time.Second, 10*time.Millisecond)
	require.Equal(t, []string{
		"see your DMs",
		"connected",
		"On it. Working in your beam; this can take a few minutes.",
		"page done",
	}, bot.replies)
	runner.mu.Lock()
	defer runner.mu.Unlock()
	require.Equal(t, []string{"C1-1.1 make a page", "D1-2.2 the-session-id", "C1-1.1 make a page"}, runner.handled)
}

func (r *blockingRunner) ThreadUsesBeam(string, string, string) bool { return true }
func (r *authRunner) ThreadUsesBeam(string, string, string) bool     { return true }

func (b *fakeBeamsBot) DownloadSlackFile(_ context.Context, fileURL string, _ int64) ([]byte, error) {
	if strings.Contains(fileURL, "forbidden") {
		return nil, trace.AccessDenied("Slack did not return the file; the app needs the files:read scope")
	}
	return []byte("file from " + fileURL), nil
}

func TestDownloadAttachments(t *testing.T) {
	app := &BeamsApp{bot: &fakeBeamsBot{}}
	files, notes := app.downloadAttachments(t.Context(), []SlackFile{
		{Name: "dog.png", Mimetype: "image/png", URLPrivate: "https://files.slack.com/dog.png"},
		{Name: "secret.pdf", Mimetype: "application/pdf", URLPrivate: "https://files.slack.com/forbidden.pdf"},
	})
	require.Equal(t, []attachment{{Name: "dog.png", MimeType: "image/png", Data: []byte("file from https://files.slack.com/dog.png")}}, files)
	require.Equal(t, []string{"I couldn't read `secret.pdf`: Slack did not return the file; the app needs the files:read scope"}, notes)
}

func TestDownloadSlackFileRefusesOtherHosts(t *testing.T) {
	_, err := Bot{}.DownloadSlackFile(t.Context(), "https://example.com/steal-token", 10)
	require.ErrorContains(t, err, "refusing to download")
	_, err = Bot{}.DownloadSlackFile(t.Context(), "http://files.slack.com/x", 10)
	require.ErrorContains(t, err, "refusing to download")
}
