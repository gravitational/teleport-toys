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
	"cmp"
	"context"
	"fmt"
	"regexp"
	"slices"
	"strings"

	"github.com/gravitational/trace"

	"github.com/gravitational/teleport/integrations/lib/logger"
)

// tellInstructions explains the tell action to the chat model and to coding
// agents.
const tellInstructions = `You can send Slack messages for the user. When they ask you to tell, notify, ping, or let someone know something, add one line per person:
` + botActionPrefix + ` tell <person> :: <message>
<person> is the name the user gave, or the <@ID> mention if they used one. Write <message> as a short note to that person with what the user asked you to pass on; if it is about a problem or incident, add that we're on it. The Slack bot sends it from the user and reports any problem, so never say you can't message people.`

// mentionID matches a Slack user mention such as <@U123> or <@U123|anna>.
var mentionID = regexp.MustCompile(`^<@([A-Z0-9]+)(?:\|[^>]*)?>$`)

// SlackPerson is a Slack user the bot can send a message to for a user.
type SlackPerson struct {
	ID          string
	Name        string
	RealName    string
	DisplayName string
}

func (p SlackPerson) label() string {
	return cmp.Or(p.RealName, p.DisplayName, p.Name, p.ID)
}

// parseTell reads a "tell <person> :: <message>" action.
func parseTell(action string) (who, message string, ok bool) {
	rest, ok := strings.CutPrefix(action, "tell ")
	if !ok {
		return "", "", false
	}
	who, message, ok = strings.Cut(rest, "::")
	who, message = strings.TrimSpace(who), strings.TrimSpace(message)
	return who, message, ok && who != "" && message != ""
}

// matchPeople finds the Slack users a name refers to. A full name or handle
// wins; otherwise a display name or any word of a name, such as a first
// name. Display names alone are often just a first name, so they don't win.
func matchPeople(people []SlackPerson, query string) []SlackPerson {
	q := strings.ToLower(strings.TrimPrefix(strings.TrimSpace(query), "@"))
	if q == "" {
		return nil
	}
	var exact, partial []SlackPerson
	for _, p := range people {
		names := []string{strings.ToLower(p.RealName), strings.ToLower(p.DisplayName), strings.ToLower(p.Name)}
		switch {
		case q == names[0] || q == names[2]:
			exact = append(exact, p)
		case slices.ContainsFunc(names, func(name string) bool {
			return slices.Contains(nameWords(name), q) || (strings.Contains(q, " ") && strings.HasPrefix(name, q))
		}):
			partial = append(partial, p)
		}
	}
	if len(exact) > 0 {
		return exact
	}
	return partial
}

func nameWords(name string) []string {
	return strings.FieldsFunc(name, func(r rune) bool {
		return r == ' ' || r == '.' || r == '_' || r == '-'
	})
}

// tell sends a message to someone for the user. From a channel thread it is
// posted in the thread with a mention, so they are notified there; from a
// direct message with the bot it goes to them directly. told records who has
// been messaged during this request so nobody gets the same note twice. It
// returns a note for the user, empty when the message in the thread speaks
// for itself.
func (s beamsSession) tell(ctx context.Context, channel, threadTS, action string, told map[string]string) string {
	who, message, ok := parseTell(action)
	if !ok {
		return ""
	}
	if s.runner.FindPeople == nil || s.runner.Post == nil {
		return "I can't send Slack messages from here."
	}
	people, err := s.runner.FindPeople(ctx, who)
	if err != nil {
		logger.Get(ctx).WarnContext(ctx, "Failed to look up Slack users", "query", who, "error", err)
		return fmt.Sprintf("I couldn't look up %s in Slack: %s", who, trace.UserMessage(err))
	}
	switch len(people) {
	case 0:
		return fmt.Sprintf("I couldn't find anyone in Slack called %s, so I didn't message them.", who)
	case 1:
	default:
		var names []string
		for _, p := range people[:min(len(people), 5)] {
			names = append(names, p.label())
		}
		return fmt.Sprintf("More than one person matches %s (%s), so I didn't message anyone. Say which one, for example \"tell %s …\".",
			who, strings.Join(names, ", "), names[0])
	}
	person := people[0]
	if _, ok := told[person.ID]; ok {
		return ""
	}

	direct := isDirectMessage(channel)
	if direct {
		err = s.runner.Post(ctx, person.ID, "", fmt.Sprintf("Message from <@%s>: %s", s.slackUserID, message))
	} else {
		err = s.runner.Post(ctx, channel, threadTS, fmt.Sprintf("<@%s> message from <@%s>: %s", person.ID, s.slackUserID, message))
	}
	if err != nil {
		logger.Get(ctx).WarnContext(ctx, "Failed to send a Slack message for the user", "to_slack_user_id", person.ID, "error", err)
		return fmt.Sprintf("I couldn't message %s: %s", person.label(), trace.UserMessage(err))
	}
	told[person.ID] = fmt.Sprintf("%s: %s", person.label(), message)
	s.logRequest(ctx, "tell", message, "to_slack_user_id", person.ID)
	if direct {
		return fmt.Sprintf("Sent %s a direct message.", person.label())
	}
	return ""
}

// alreadyTold tells a coding agent which messages were sent before it ran.
func alreadyTold(told map[string]string) string {
	if len(told) == 0 {
		return ""
	}
	lines := make([]string, 0, len(told))
	for _, line := range told {
		lines = append(lines, "- "+line)
	}
	slices.Sort(lines)
	return "The Slack bot already sent these messages for the user, so don't send them again:\n" + strings.Join(lines, "\n") + "\n"
}
