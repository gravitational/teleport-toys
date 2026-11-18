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
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/gravitational/trace"
)

// Slack calls the Beams app makes through the plugin's Bot.

// PostBeamsReply posts a private response through the response URL attached to
// an authenticated Slack slash-command event.
func (b Bot) PostBeamsReply(ctx context.Context, responseURL, text string) error {
	u, err := url.Parse(responseURL)
	if err != nil {
		return trace.Wrap(err)
	}
	if u.Scheme != "https" || (u.Hostname() != "hooks.slack.com" && u.Hostname() != "hooks.slack-gov.com") {
		return trace.BadParameter("invalid Slack response URL")
	}

	body, err := json.Marshal(map[string]string{
		"response_type": "ephemeral",
		"text":          text,
	})
	if err != nil {
		return trace.Wrap(err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u.String(), bytes.NewReader(body))
	if err != nil {
		return trace.Wrap(err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := b.client.GetClient().Do(req)
	if err != nil {
		return trace.Wrap(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		responseBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return trace.Errorf("Slack response URL returned %s: %s", resp.Status, strings.TrimSpace(string(responseBody)))
	}
	return nil
}

// slackFileHosts are the hosts Slack serves message attachments from. The bot
// token is only ever sent to these.
var slackFileHosts = map[string]bool{"files.slack.com": true, "files-origin.slack.com": true}

// DownloadSlackFile fetches a message attachment with the bot token, which
// needs the files:read scope. It reads at most limit bytes.
func (b Bot) DownloadSlackFile(ctx context.Context, fileURL string, limit int64) ([]byte, error) {
	u, err := url.Parse(fileURL)
	if err != nil {
		return nil, trace.Wrap(err)
	}
	if u.Scheme != "https" || !slackFileHosts[u.Hostname()] {
		return nil, trace.BadParameter("refusing to download a file from %q", u.Hostname())
	}
	if b.botToken == nil {
		return nil, trace.BadParameter("no Slack bot token")
	}
	token, err := b.botToken.GetAccessToken()
	if err != nil {
		return nil, trace.Wrap(err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, trace.Wrap(err)
	}
	req.Header.Set("Authorization", "Bearer "+token)
	// The Slack API client's response hooks expect JSON, so use its plain
	// HTTP client for file downloads.
	resp, err := b.client.GetClient().Do(req)
	if err != nil {
		return nil, trace.Wrap(err)
	}
	body := resp.Body
	defer body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, trace.Errorf("Slack returned %s for the file", resp.Status)
	}
	if strings.HasPrefix(resp.Header.Get("Content-Type"), "text/html") {
		// Slack answers with its login page when the token lacks files:read.
		return nil, trace.AccessDenied("Slack did not return the file; the app needs the files:read scope")
	}
	data, err := io.ReadAll(io.LimitReader(body, limit+1))
	if err != nil {
		return nil, trace.Wrap(err)
	}
	if int64(len(data)) > limit {
		return nil, trace.LimitExceeded("file is larger than %d bytes", limit)
	}
	return data, nil
}

// TeleportClusterName returns the name of the Teleport cluster the plugin
// is connected to.
func (b Bot) TeleportClusterName() string {
	return b.clusterName
}

// BotUserID returns the bot's own Slack user ID.
func (b Bot) BotUserID(ctx context.Context) (string, error) {
	var result struct {
		APIResponse
		UserID string `json:"user_id"`
	}
	_, err := b.client.NewRequest().
		SetContext(ctx).
		SetResult(&result).
		Post("auth.test")
	return result.UserID, trace.Wrap(err)
}

// FindSlackPeople finds the active people in the workspace a name or <@ID>
// mention refers to.
func (b Bot) FindSlackPeople(ctx context.Context, query string) ([]SlackPerson, error) {
	if id := mentionID.FindStringSubmatch(strings.TrimSpace(query)); id != nil {
		var result struct {
			APIResponse
			User SlackMember `json:"user"`
		}
		_, err := b.client.NewRequest().
			SetContext(ctx).
			SetQueryParam("user", id[1]).
			SetResult(&result).
			Get("users.info")
		if err != nil {
			return nil, trace.Wrap(err)
		}
		return []SlackPerson{result.User.person()}, nil
	}

	var people []SlackPerson
	cursor := ""
	for {
		var result UsersListResponse
		req := b.client.NewRequest().
			SetContext(ctx).
			SetQueryParam("limit", "200").
			SetResult(&result)
		if cursor != "" {
			req.SetQueryParam("cursor", cursor)
		}
		if _, err := req.Get("users.list"); err != nil {
			return nil, trace.Wrap(err)
		}
		for _, member := range result.Members {
			if member.Deleted || member.IsBot || member.ID == "USLACKBOT" {
				continue
			}
			people = append(people, member.person())
		}
		if cursor = result.ResponseMetadata.NextCursor; cursor == "" {
			break
		}
	}
	return matchPeople(people, query), nil
}

// PostBeamsThreadReply posts a message in a Slack thread.
func (b Bot) PostBeamsThreadReply(ctx context.Context, channelID, threadTS, text string) error {
	_, err := b.client.NewRequest().
		SetContext(ctx).
		SetBody(Message{BaseMessage: BaseMessage{Channel: channelID, ThreadTs: threadTS}, Text: text}).
		Post("chat.postMessage")
	return trace.Wrap(err)
}
