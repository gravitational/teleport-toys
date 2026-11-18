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
	"encoding/json"

	"github.com/gravitational/trace"
)

const (
	SocketModeEventTypeSlashCommands = "slash_commands"
	SocketModeEventTypeEventsAPI     = "events_api"
)

// UsersListResponse is a page of users.list.
type UsersListResponse struct {
	APIResponse
	Members          []SlackMember `json:"members"`
	ResponseMetadata struct {
		NextCursor string `json:"next_cursor"`
	} `json:"response_metadata"`
}

// SlackMember is a user in users.list or users.info.
type SlackMember struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Deleted bool   `json:"deleted"`
	IsBot   bool   `json:"is_bot"`
	Profile struct {
		RealName    string `json:"real_name"`
		DisplayName string `json:"display_name"`
	} `json:"profile"`
}

func (m SlackMember) person() SlackPerson {
	return SlackPerson{ID: m.ID, Name: m.Name, RealName: m.Profile.RealName, DisplayName: m.Profile.DisplayName}
}

// BeamsMessageEvent is an app_mention or message event delivered through the
// Slack Events API over Socket Mode.
type BeamsMessageEvent struct {
	TeamID      string
	EventType   string
	User        string
	BotID       string
	Subtype     string
	Text        string
	Channel     string
	ChannelType string
	TS          string
	ThreadTS    string
	Files       []SlackFile
}

// SlackFile is a file attached to a Slack message.
type SlackFile struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	Mimetype   string `json:"mimetype"`
	Size       int64  `json:"size"`
	URLPrivate string `json:"url_private"`
}

func (BeamsMessageEvent) Type() InteractionEventType {
	return SocketModeEventTypeEventsAPI
}

// UnmarshalBeamsMessageEvent decodes an events_api payload.
func UnmarshalBeamsMessageEvent(data []byte) (BeamsMessageEvent, error) {
	var payload struct {
		TeamID string `json:"team_id"`
		Event  struct {
			Type        string      `json:"type"`
			User        string      `json:"user"`
			BotID       string      `json:"bot_id"`
			Subtype     string      `json:"subtype"`
			Text        string      `json:"text"`
			Channel     string      `json:"channel"`
			ChannelType string      `json:"channel_type"`
			TS          string      `json:"ts"`
			ThreadTS    string      `json:"thread_ts"`
			Files       []SlackFile `json:"files"`
		} `json:"event"`
	}
	if err := json.Unmarshal(data, &payload); err != nil {
		return BeamsMessageEvent{}, trace.Wrap(err)
	}
	e := payload.Event
	return BeamsMessageEvent{
		TeamID:      payload.TeamID,
		EventType:   e.Type,
		User:        e.User,
		BotID:       e.BotID,
		Subtype:     e.Subtype,
		Text:        e.Text,
		Channel:     e.Channel,
		ChannelType: e.ChannelType,
		TS:          e.TS,
		ThreadTS:    e.ThreadTS,
		Files:       e.Files,
	}, nil
}

// SlashCommandEvent is delivered by Slack Socket Mode for a slash command.
type SlashCommandEvent struct {
	PayloadType string `json:"type"`
	Command     string `json:"command"`
	Text        string `json:"text"`
	ResponseURL string `json:"response_url"`
	TriggerID   string `json:"trigger_id"`
	UserID      string `json:"user_id"`
	UserName    string `json:"user_name"`
	TeamID      string `json:"team_id"`
	TeamDomain  string `json:"team_domain"`
	ChannelID   string `json:"channel_id"`
	ChannelName string `json:"channel_name"`
}

func (SlashCommandEvent) Type() InteractionEventType {
	return InteractionEventType("slash_commands")
}
