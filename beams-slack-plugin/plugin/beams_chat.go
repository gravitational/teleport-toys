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
	"cmp"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/gravitational/trace"

	"github.com/gravitational/teleport/api/client/proto"
	delegationv1 "github.com/gravitational/teleport/api/gen/proto/go/teleport/delegation/v1"
	"github.com/gravitational/teleport/api/types"
	"github.com/gravitational/teleport/api/utils/keys"
	"github.com/gravitational/teleport/integrations/lib/logger"
)

const (
	// llmAppLabel marks the Teleport apps that proxy model APIs for beams.
	llmAppLabel = "teleport.internal/beams/app-type"
	// chatHandoff is the reply that means a request needs a coding agent in a
	// beam rather than a chat answer.
	chatHandoff = "HANDOFF"
	// chatHistoryLimit is how many chat messages a thread keeps for context.
	chatHistoryLimit = 20
	chatMaxTokens    = 1024
	// chatMaxReasoningTokens leaves reasoning models room to think before
	// answering.
	chatMaxReasoningTokens = 4096
	chatTimeout            = 2 * time.Minute

	defaultChatClaudeModel = "claude-sonnet-4-5"
	defaultChatCodexModel  = "gpt-5"
)

// appLister lists Teleport resources; the plugin's Teleport client satisfies it.
type appLister interface {
	ListResources(ctx context.Context, req proto.ListResourcesRequest) (*types.ListResourcesResponse, error)
}

// llmApp is a Teleport app that proxies a model provider's API. Teleport adds
// the provider credentials and records every request in the audit log.
type llmApp struct {
	Name       string
	PublicAddr string
}

// chatMessage is one turn of a chat thread. Images go to the model with the
// message but are not kept in the thread's history.
type chatMessage struct {
	Role    string       `json:"role"`
	Content string       `json:"content"`
	Images  []attachment `json:"-"`
}

func (r BeamsCommandRunner) chatEnabled() bool {
	return !r.Config.DisableChat && (r.Apps != nil || r.Chat != nil)
}

// chatSystemPrompt tells the model to answer conversational requests and hand
// anything that needs a computer to a coding agent in a beam.
func chatSystemPrompt(username string) string {
	return fmt.Sprintf(`You are Teleport Beams Bot, a friendly assistant in Slack, talking with %s. Answer conversationally and concisely, using Slack formatting (*bold*, _italic_, `+"`code`"+`, and • for bullets).

Some requests need a computer: running or testing code for the user; building or editing files, webpages, or apps; installing software; publishing something; or creating, listing, or deleting Teleport Beams. For those, reply with exactly %s and nothing else (apart from any tell lines), and a coding agent in a beam will take over.
Answer directly instead when the user wants a command or a short script they can run themselves, such as a quick curl command to check whether a website is up: put it in a code block with a line on how to use it. Hand off only when something has to be run, built, or saved for them, or the code is too long for a Slack message.

`, username, chatHandoff) + tellInstructions + `
Put tell lines first. After them, answer the rest of the request, or reply with only ` + chatHandoff + ` if it needs a computer. If passing the message on was the whole request, a short acknowledgement is enough.`
}

// chatTurn answers a request in a chat thread. It returns handoff when the
// request needs a coding agent in a beam instead, along with the earlier
// conversation so the agent has context.
//
// actions are the tell actions the model asked for, to carry out either way.
func (s beamsSession) chatTurn(ctx context.Context, historyPath, agent, text string, images []attachment) (reply string, handoff bool, history []chatMessage, actions []string, err error) {
	history = readChatHistory(historyPath)
	messages := append(append([]chatMessage{}, history...), chatMessage{Role: "user", Content: text, Images: images})

	chat := s.runner.Chat
	if chat == nil {
		chat = s.callLLMApp
	}
	reply, err = chat(ctx, s, agent, messages)
	if err != nil {
		return "", false, history, nil, trace.Wrap(err)
	}
	reply, actions = parseBotActions(reply)
	if isHandoff(reply) {
		return "", true, history, actions, nil
	}

	// Keep only a note of the images in the history.
	updated := append(append([]chatMessage{}, history...),
		chatMessage{Role: "user", Content: describeAttachments(text, images)},
		chatMessage{Role: "assistant", Content: reply},
	)
	if len(updated) > chatHistoryLimit {
		updated = updated[len(updated)-chatHistoryLimit:]
	}
	if err := writeChatHistory(historyPath, updated); err != nil {
		return "", false, history, nil, trace.Wrap(err)
	}
	return reply, false, history, actions, nil
}

// isHandoff reports whether a chat reply hands the request to a coding agent.
// Models sometimes add a sentence before the marker, so any line that is just
// the marker counts.
func isHandoff(reply string) bool {
	for _, line := range strings.Split(reply, "\n") {
		if strings.Trim(line, " \t*_`.") == chatHandoff {
			return true
		}
	}
	return false
}

func readChatHistory(path string) []chatMessage {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var history []chatMessage
	if json.Unmarshal(data, &history) != nil {
		return nil
	}
	return history
}

func writeChatHistory(path string, messages []chatMessage) error {
	data, err := json.Marshal(messages)
	if err != nil {
		return trace.Wrap(err)
	}
	return trace.Wrap(os.WriteFile(path, data, 0o600))
}

// chatContext renders earlier chat turns for a coding agent's prompt.
func chatContext(history []chatMessage) string {
	var b strings.Builder
	for _, m := range history {
		fmt.Fprintf(&b, "%s: %s\n", m.Role, m.Content)
	}
	return b.String()
}

// callLLMApp sends the chat to the Teleport LLM app for the agent, as the
// user: the delegation API mints a certificate for the app and starts an app
// session, so Teleport attributes and audits the request.
func (s beamsSession) callLLMApp(ctx context.Context, _ beamsSession, agent string, messages []chatMessage) (string, error) {
	appName := "anthropic"
	if agent == agentCodex {
		appName = "openai"
	}
	app, err := s.runner.findLLMApp(ctx, appName)
	if err != nil {
		return "", trace.Wrap(err)
	}
	cert, sessionID, err := s.appCertificate(ctx, app)
	if err != nil {
		return "", trace.Wrap(err)
	}
	model := cmp.Or(s.runner.Config.ChatClaudeModel, defaultChatClaudeModel)
	if agent == agentCodex {
		model = cmp.Or(s.runner.Config.ChatCodexModel, defaultChatCodexModel)
	}
	last := messages[len(messages)-1]
	s.logRequest(ctx, "chat", last.Content,
		"agent", agent, "app", app.Name, "model", model,
		"app_session_id", sessionID, "attachments", attachmentNames(last.Images))
	client := &http.Client{
		Timeout:   chatTimeout,
		Transport: &http.Transport{TLSClientConfig: &tls.Config{Certificates: []tls.Certificate{cert}}},
	}
	baseURL := "https://" + app.PublicAddr
	system := chatSystemPrompt(s.username)
	if agent == agentCodex {
		return openAIChat(ctx, client, baseURL, model, system, messages)
	}
	return anthropicChat(ctx, client, baseURL, model, system, messages)
}

// findLLMApp looks up a Teleport LLM app by name.
func (r BeamsCommandRunner) findLLMApp(ctx context.Context, name string) (llmApp, error) {
	if r.Apps == nil {
		return llmApp{}, trace.NotImplemented("no Teleport client for listing apps")
	}
	resp, err := r.Apps.ListResources(ctx, proto.ListResourcesRequest{
		ResourceType: types.KindAppServer,
		Labels:       map[string]string{llmAppLabel: "llm"},
		Limit:        50,
	})
	if err != nil {
		return llmApp{}, trace.Wrap(err)
	}
	for _, resource := range resp.Resources {
		server, ok := resource.(types.AppServer)
		if !ok {
			continue
		}
		if app := server.GetApp(); app.GetName() == name {
			return llmApp{Name: app.GetName(), PublicAddr: app.GetPublicAddr()}, nil
		}
	}
	return llmApp{}, trace.NotFound("no Teleport LLM app named %q", name)
}

// appCertificate mints a TLS client certificate for the app as the user and
// returns it with the Teleport app session it opened.
func (s beamsSession) appCertificate(ctx context.Context, app llmApp) (tls.Certificate, string, error) {
	if s.delegation == "" {
		return tls.Certificate{}, "", &needsAuthorizationError{trace.AccessDenied("not connected")}
	}
	privateKey, response, err := s.runner.mintDelegatedCerts(ctx, s.delegation, s.username, &delegationv1.RouteToApp{
		Name:        app.Name,
		PublicAddr:  app.PublicAddr,
		ClusterName: s.runner.ClusterName,
	})
	if trace.IsAccessDenied(err) || trace.IsNotFound(err) {
		return tls.Certificate{}, "", &needsAuthorizationError{err}
	}
	if err != nil {
		return tls.Certificate{}, "", trace.Wrap(err)
	}
	keyPEM, err := keys.MarshalPrivateKey(privateKey)
	if err != nil {
		return tls.Certificate{}, "", trace.Wrap(err)
	}
	cert, err := tls.X509KeyPair(response.GetTls(), keyPEM)
	if err != nil {
		return tls.Certificate{}, "", trace.Wrap(err)
	}
	sessionID, err := appSessionID(response.GetTls())
	if err != nil {
		// The request still works; only the log loses the session ID.
		logger.Get(ctx).WarnContext(ctx, "Could not read the app session ID", "error", err)
	}
	return cert, sessionID, nil
}

// anthropicChat calls the Messages API. Teleport replaces the API key.
func anthropicChat(ctx context.Context, client *http.Client, baseURL, model, system string, messages []chatMessage) (string, error) {
	var payload []map[string]any
	for _, m := range messages {
		if len(m.Images) == 0 {
			payload = append(payload, map[string]any{"role": m.Role, "content": m.Content})
			continue
		}
		var content []map[string]any
		for _, img := range m.Images {
			content = append(content, map[string]any{
				"type":   "image",
				"source": map[string]any{"type": "base64", "media_type": img.MimeType, "data": img.base64()},
			})
		}
		content = append(content, map[string]any{"type": "text", "text": m.Content})
		payload = append(payload, map[string]any{"role": m.Role, "content": content})
	}
	body := map[string]any{
		"model":      model,
		"max_tokens": chatMaxTokens,
		"system":     system,
		"messages":   payload,
	}
	headers := map[string]string{"x-api-key": "teleport", "anthropic-version": "2023-06-01"}
	var resp struct {
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
	}
	if err := postJSON(ctx, client, baseURL+"/v1/messages", headers, body, &resp); err != nil {
		return "", trace.Wrap(err)
	}
	var text []string
	for _, block := range resp.Content {
		if block.Type == "text" {
			text = append(text, block.Text)
		}
	}
	return strings.Join(text, ""), nil
}

// openAIChat calls the Responses API, the route Teleport's OpenAI app serves
// for every model (Bedrock-hosted OpenAI models are not available on Chat
// Completions). Teleport replaces the API key.
func openAIChat(ctx context.Context, client *http.Client, baseURL, model, system string, messages []chatMessage) (string, error) {
	var input []map[string]any
	for _, m := range messages {
		if len(m.Images) == 0 {
			input = append(input, map[string]any{"role": m.Role, "content": m.Content})
			continue
		}
		content := []map[string]any{{"type": "input_text", "text": m.Content}}
		for _, img := range m.Images {
			content = append(content, map[string]any{
				"type":      "input_image",
				"image_url": "data:" + img.MimeType + ";base64," + img.base64(),
			})
		}
		input = append(input, map[string]any{"role": m.Role, "content": content})
	}
	body := map[string]any{
		"model":        model,
		"instructions": system,
		"input":        input,
		// Reasoning models spend part of this budget thinking.
		"max_output_tokens": chatMaxReasoningTokens,
		"store":             false,
	}
	headers := map[string]string{"Authorization": "Bearer teleport"}
	var resp struct {
		Status string `json:"status"`
		Output []struct {
			Type    string `json:"type"`
			Content []struct {
				Type string `json:"type"`
				Text string `json:"text"`
			} `json:"content"`
		} `json:"output"`
	}
	if err := postJSON(ctx, client, baseURL+"/v1/responses", headers, body, &resp); err != nil {
		return "", trace.Wrap(err)
	}
	var text []string
	for _, item := range resp.Output {
		if item.Type != "message" {
			continue
		}
		for _, part := range item.Content {
			if part.Type == "output_text" {
				text = append(text, part.Text)
			}
		}
	}
	if len(text) == 0 {
		return "", trace.BadParameter("model returned no answer (status %q)", resp.Status)
	}
	return strings.Join(text, ""), nil
}

func postJSON(ctx context.Context, client *http.Client, url string, headers map[string]string, body, out any) error {
	payload, err := json.Marshal(body)
	if err != nil {
		return trace.Wrap(err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return trace.Wrap(err)
	}
	req.Header.Set("content-type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := client.Do(req)
	if err != nil {
		return trace.Wrap(err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return trace.Wrap(err)
	}
	if resp.StatusCode != http.StatusOK {
		return trace.Errorf("model API returned %s: %s", resp.Status, strings.TrimSpace(string(data[:min(len(data), 500)])))
	}
	return trace.Wrap(json.Unmarshal(data, out))
}

// chatHistoryPath is where a thread's chat messages are kept.
func chatHistoryPath(threadsDir, threadKey string) string {
	return filepath.Join(threadsDir, threadKey+".chat")
}
