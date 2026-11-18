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
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gravitational/trace"
	"github.com/stretchr/testify/require"

	"github.com/gravitational/teleport/api/client/proto"
	"github.com/gravitational/teleport/api/types"
)

// fakeChat answers lunch-style questions and hands off anything to build.
type fakeChat struct {
	mu    sync.Mutex
	calls []struct {
		agent    string
		messages []chatMessage
	}
}

func (f *fakeChat) chat(_ context.Context, _ beamsSession, agent string, messages []chatMessage) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, struct {
		agent    string
		messages []chatMessage
	}{agent, messages})
	if strings.Contains(messages[len(messages)-1].Content, "webpage") {
		return chatHandoff, nil
	}
	return "Try the pho place.", nil
}

func TestBotChatAnswersWithoutBeam(t *testing.T) {
	tshPath, logPath := writeFakeTSH(t)
	chat := &fakeChat{}
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:     tshPath,
			ProfilesDir: t.TempDir(),
			BotName:     "teleport-beams-bot",
			Users:       map[string]string{"UALICE": "alice@example.com"},
		},
		MintIdentity: fakeMint,
		Chat:         chat.chat,
	}
	waitForBackground(t, &runner)
	ctx := t.Context()
	_, err := runner.Run(ctx, "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)
	thread := "C1-1700000000.002000"

	// A conversational question is answered directly, without a beam.
	out, err := runner.Ask(ctx, "T1", "UALICE", thread, "what should i eat for lunch")
	require.NoError(t, err)
	require.Equal(t, "Try the pho place.", out)
	require.True(t, runner.FollowsThread("T1", "UALICE", thread))

	// Follow-ups carry the conversation.
	_, err = runner.Ask(ctx, "T1", "UALICE", thread, "what about dinner")
	require.NoError(t, err)
	require.Len(t, chat.calls[1].messages, 3)
	require.Equal(t, "assistant", chat.calls[1].messages[1].Role)

	calls, err := os.ReadFile(logPath)
	require.NoError(t, err)
	require.NotContains(t, string(calls), "'claude'", "chat answers must not start a coding agent")

	// A request to build something is handed to a coding agent in a beam,
	// with the chat so far as context.
	_, err = runner.Ask(ctx, "T1", "UALICE", thread, "make a webpage about it")
	require.NoError(t, err)
	calls, err = os.ReadFile(logPath)
	require.NoError(t, err)
	require.Contains(t, string(calls), "beams exec mine")
	require.Contains(t, string(calls), "Earlier in this Slack conversation:")
	require.Contains(t, string(calls), "user: what should i eat for lunch")

	// "use codex" chats through the OpenAI app.
	_, err = runner.Ask(ctx, "T1", "UALICE", "C1-1700000000.002100", "what should i eat for lunch, use codex")
	require.NoError(t, err)
	require.Equal(t, agentCodex, chat.calls[len(chat.calls)-1].agent)

	// A model error is reported instead of starting beam work.
	failing := runner
	failing.Chat = func(context.Context, beamsSession, string, []chatMessage) (string, error) {
		return "", trace.Errorf("model API returned 400 Bad Request: not supported on this route")
	}
	_, err = failing.Ask(ctx, "T1", "UALICE", "C1-1700000000.002300", "what should i eat")
	require.ErrorContains(t, err, "not supported on this route")

	// Without LLM apps, chat falls back to a coding agent in a beam.
	noApps := runner
	noApps.Chat = func(context.Context, beamsSession, string, []chatMessage) (string, error) {
		return "", trace.NotFound("no Teleport LLM app named \"anthropic\"")
	}
	_, err = noApps.Ask(ctx, "T1", "UALICE", "C1-1700000000.002400", "what should i eat")
	require.NoError(t, err)

	// Naming a beam goes straight to the agent working in it.
	before := len(chat.calls)
	_, err = runner.Ask(ctx, "T1", "UALICE", "C1-1700000000.002200", "what's running in mine")
	require.NoError(t, err)
	require.Len(t, chat.calls, before)
}

func TestAnthropicChat(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/v1/messages", r.URL.Path)
		require.Equal(t, "teleport", r.Header.Get("x-api-key"))
		require.Equal(t, "2023-06-01", r.Header.Get("anthropic-version"))
		var body map[string]any
		require.NoError(t, json.NewDecoder(r.Body).Decode(&body))
		require.Equal(t, "claude-test", body["model"])
		require.Equal(t, "be brief", body["system"])
		require.Len(t, body["messages"], 1)
		io.WriteString(w, `{"content":[{"type":"text","text":"Hello"},{"type":"text","text":" there"}]}`)
	}))
	defer server.Close()

	out, err := anthropicChat(t.Context(), server.Client(), server.URL, "claude-test", "be brief", []chatMessage{{Role: "user", Content: "hi"}})
	require.NoError(t, err)
	require.Equal(t, "Hello there", out)
}

func TestOpenAIChat(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/v1/responses", r.URL.Path)
		require.Equal(t, "Bearer teleport", r.Header.Get("Authorization"))
		var body struct {
			Model        string        `json:"model"`
			Instructions string        `json:"instructions"`
			Input        []chatMessage `json:"input"`
			Store        bool          `json:"store"`
		}
		require.NoError(t, json.NewDecoder(r.Body).Decode(&body))
		require.Equal(t, "gpt-test", body.Model)
		require.Equal(t, "be brief", body.Instructions)
		require.Equal(t, []chatMessage{{Role: "user", Content: "hi"}}, body.Input)
		require.False(t, body.Store)
		io.WriteString(w, `{"status":"completed","output":[{"type":"reasoning"},{"type":"message","content":[{"type":"output_text","text":"Hello"}]}]}`)
	}))
	defer server.Close()

	out, err := openAIChat(t.Context(), server.Client(), server.URL, "gpt-test", "be brief", []chatMessage{{Role: "user", Content: "hi"}})
	require.NoError(t, err)
	require.Equal(t, "Hello", out)

	empty := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		io.WriteString(w, `{"status":"incomplete","output":[{"type":"reasoning"}]}`)
	}))
	defer empty.Close()
	_, err = openAIChat(t.Context(), empty.Client(), empty.URL, "gpt-test", "", nil)
	require.ErrorContains(t, err, `status "incomplete"`)

	failing := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `model isn't supported on this route`, http.StatusBadRequest)
	}))
	defer failing.Close()
	_, err = openAIChat(t.Context(), failing.Client(), failing.URL, "gpt-test", "", nil)
	require.ErrorContains(t, err, "isn't supported on this route")
}

type fakeAppLister []types.ResourceWithLabels

func (f fakeAppLister) ListResources(_ context.Context, req proto.ListResourcesRequest) (*types.ListResourcesResponse, error) {
	return &types.ListResourcesResponse{Resources: f}, nil
}

func TestFindLLMApp(t *testing.T) {
	var servers fakeAppLister
	for _, name := range []string{"anthropic", "openai"} {
		app, err := types.NewAppV3(types.Metadata{Name: name, Labels: map[string]string{llmAppLabel: "llm"}},
			types.AppSpecV3{URI: "http://localhost", PublicAddr: name + ".example.teleport.sh"})
		require.NoError(t, err)
		server, err := types.NewAppServerV3FromApp(app, "host", "host-id")
		require.NoError(t, err)
		servers = append(servers, server)
	}
	runner := BeamsCommandRunner{Apps: servers}
	app, err := runner.findLLMApp(t.Context(), "openai")
	require.NoError(t, err)
	require.Equal(t, llmApp{Name: "openai", PublicAddr: "openai.example.teleport.sh"}, app)
	_, err = runner.findLLMApp(t.Context(), "gemini")
	require.Error(t, err)
}

func TestBotAcknowledgesChatAndBeamWorkDifferently(t *testing.T) {
	tshPath, _ := writeFakeTSH(t)
	chat := &fakeChat{}
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:     tshPath,
			ProfilesDir: t.TempDir(),
			BotName:     "teleport-beams-bot",
			Users:       map[string]string{"UALICE": "alice@example.com"},
		},
		MintIdentity: fakeMint,
		Chat:         chat.chat,
	}
	waitForBackground(t, &runner)
	_, err := runner.Run(t.Context(), "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)
	bot := &fakeBeamsBot{}
	app := &BeamsApp{bot: bot, runner: runner, seen: newRecentSet(10), threads: newThreadQueue()}
	event := BeamsMessageEvent{TeamID: "T1", User: "UALICE", Channel: "C1", TS: "1700000000.003000"}

	// A chat question gets a short acknowledgement and the answer.
	app.handleMessage(t.Context(), event, "what should i eat for lunch")
	require.Equal(t, []string{"Just a moment while I look into this.", "Try the pho place."}, bot.replies)

	// When it turns into beam work, the bot says so before the agent runs.
	bot.replies = nil
	event.TS = "1700000000.003100"
	event.ThreadTS = "1700000000.003000"
	app.handleMessage(t.Context(), event, "make a webpage about it")
	require.Equal(t, "Just a moment while I look into this.", bot.replies[0])
	require.Equal(t, "This needs a computer, so I'm working in one of your beams. This can take a few minutes.", bot.replies[1])

	// From then on the thread works in its beam.
	bot.replies = nil
	event.TS = "1700000000.003200"
	app.handleMessage(t.Context(), event, "make the title bigger")
	require.Equal(t, beamWorkReply, bot.replies[0])
	require.Len(t, bot.replies, 2)
}

func TestChatImages(t *testing.T) {
	png := attachment{Name: "a.png", MimeType: "image/png", Data: []byte("x")}
	images, ok := chatImages([]attachment{png})
	require.True(t, ok)
	require.Len(t, images, 1)

	_, ok = chatImages([]attachment{{Name: "notes.txt", MimeType: "text/plain"}})
	require.False(t, ok, "other file types need a beam")
	_, ok = chatImages([]attachment{{Name: "big.png", MimeType: "image/png", Data: make([]byte, maxChatImageSize+1)}})
	require.False(t, ok, "large images need a beam")
	_, ok = chatImages([]attachment{png, png, png})
	require.False(t, ok, "too many images need a beam")
	images, ok = chatImages(nil)
	require.True(t, ok)
	require.Empty(t, images)
}

func TestChatImagePayloads(t *testing.T) {
	img := attachment{Name: "dog.png", MimeType: "image/png", Data: []byte("png-bytes")}
	messages := []chatMessage{{Role: "user", Content: "what is this?", Images: []attachment{img}}}

	anthropic := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Messages []struct {
				Content []map[string]any `json:"content"`
			} `json:"messages"`
		}
		require.NoError(t, json.NewDecoder(r.Body).Decode(&body))
		content := body.Messages[0].Content
		require.Equal(t, "image", content[0]["type"])
		require.Equal(t, map[string]any{"type": "base64", "media_type": "image/png", "data": img.base64()}, content[0]["source"])
		require.Equal(t, map[string]any{"type": "text", "text": "what is this?"}, content[1])
		io.WriteString(w, `{"content":[{"type":"text","text":"A dog."}]}`)
	}))
	defer anthropic.Close()
	out, err := anthropicChat(t.Context(), anthropic.Client(), anthropic.URL, "m", "", messages)
	require.NoError(t, err)
	require.Equal(t, "A dog.", out)

	openai := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Input []struct {
				Content []map[string]any `json:"content"`
			} `json:"input"`
		}
		require.NoError(t, json.NewDecoder(r.Body).Decode(&body))
		content := body.Input[0].Content
		require.Equal(t, map[string]any{"type": "input_text", "text": "what is this?"}, content[0])
		require.Equal(t, map[string]any{"type": "input_image", "image_url": "data:image/png;base64," + img.base64()}, content[1])
		io.WriteString(w, `{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"A dog."}]}]}`)
	}))
	defer openai.Close()
	out, err = openAIChat(t.Context(), openai.Client(), openai.URL, "m", "", messages)
	require.NoError(t, err)
	require.Equal(t, "A dog.", out)
}

func TestBotUsesAttachments(t *testing.T) {
	tshPath, logPath := writeFakeTSH(t)
	chat := &fakeChat{}
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:     tshPath,
			ProfilesDir: t.TempDir(),
			BotName:     "teleport-beams-bot",
			Users:       map[string]string{"UALICE": "alice@example.com"},
		},
		MintIdentity: fakeMint,
		Chat:         chat.chat,
	}
	waitForBackground(t, &runner)
	ctx := t.Context()
	_, err := runner.Run(ctx, "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)

	// An image goes to the model with the question; history keeps a note.
	img := attachment{Name: "dog.png", MimeType: "image/png", Data: []byte("png")}
	thread := "C1-1700000000.004000"
	out, err := runner.Ask(withAttachments(ctx, []attachment{img}), "T1", "UALICE", thread, "tell me about this image")
	require.NoError(t, err)
	require.Equal(t, "Try the pho place.", out)
	last := chat.calls[0].messages[len(chat.calls[0].messages)-1]
	require.Equal(t, []attachment{img}, last.Images)
	profile := filepath.Join(runner.Config.ProfilesDir, "T1", "UALICE", "threads")
	history := readChatHistory(chatHistoryPath(profile, thread))
	require.Equal(t, "tell me about this image\n[attached dog.png]", history[0].Content)
	require.Empty(t, history[0].Images)

	// Other files go to a beam: copied in, with their path in the prompt.
	notes := attachment{Name: "my notes.txt", MimeType: "text/plain", Data: []byte("todo")}
	_, err = runner.Ask(withAttachments(ctx, []attachment{notes}), "T1", "UALICE", "C1-1700000000.004100", "summarize these notes")
	require.NoError(t, err)
	require.Len(t, chat.calls, 1, "non-image files skip chat")
	calls, err := os.ReadFile(logPath)
	require.NoError(t, err)
	require.Contains(t, string(calls), "beams exec mine mkdir -p /tmp/slack-attachments")
	require.Regexp(t, `beams scp \S+/upload-\d+ mine:/tmp/slack-attachments/my_notes.txt`, string(calls))
	require.Contains(t, string(calls), "- /tmp/slack-attachments/my_notes.txt")
}

// waitForBackground makes the test wait for work the runner leaves running,
// such as recording a chat answer in a beam, before its temporary
// directories are removed. Call it after creating them.
func waitForBackground(t *testing.T, r *BeamsCommandRunner) {
	var wg sync.WaitGroup
	r.Background = wg.Go
	t.Cleanup(wg.Wait)
}

func TestChatRecordedInBeamAudit(t *testing.T) {
	tshPath, logPath := writeFakeTSH(t)
	chat := &fakeChat{}
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:     tshPath,
			ProfilesDir: t.TempDir(),
			BotName:     "teleport-beams-bot",
			Users:       map[string]string{"UALICE": "alice@example.com"},
		},
		MintIdentity: fakeMint,
		Chat:         chat.chat,
	}
	waitForBackground(t, &runner)
	ctx := t.Context()
	_, err := runner.Run(ctx, "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)

	_, err = runner.Ask(ctx, "T1", "UALICE", "C1-1700000000.006000", "what's for lunch?")
	require.NoError(t, err)
	// The fake tsh logs with echo, which expands the \n in the printf format.
	require.Eventually(t, func() bool {
		calls, _ := os.ReadFile(logPath)
		return strings.Contains(string(calls), `beams exec mine 'printf'`) &&
			strings.Contains(string(calls), `'Teleport Beams Bot chat (Claude Code)' 'Q: what'\''s for lunch?' 'A: Try the pho place.'`)
	}, 5*time.Second, 20*time.Millisecond)
}

func TestIsHandoff(t *testing.T) {
	require.True(t, isHandoff("HANDOFF"))
	require.True(t, isHandoff("Got it, I've let Anna know. Now let's write that script.\n\nHANDOFF"))
	require.True(t, isHandoff("*HANDOFF*"))
	require.False(t, isHandoff("Run this:\n```\ncurl -sS -o /dev/null -w '%{http_code}' https://example.com\n```"))
	require.False(t, isHandoff("I'll do the HANDOFF later"))
}
