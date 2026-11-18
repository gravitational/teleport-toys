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
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"os"
	"regexp"
	"strings"

	"github.com/gravitational/trace"

	"github.com/gravitational/teleport/integrations/lib/logger"
)

// Scripted incident response, on only with incident_response. The incident
// number is a reference for the Slack thread, not a ticket in another system.
// The website check is real: tsh runs inside one of the user's beams,
// where it is logged in as the user, so every step is audited as them.
var (
	// incidentNotify finds who to tell about the issue: "notify the service
	// owner", "tell Alex that...", "let Alex Rivera know", "send a DM to Alex".
	incidentNotify      = regexp.MustCompile(`(?i)\b(?:tell|notify|message|dm|ping)\s+(?:to\s+)?(?:the\s+)?(<@[A-Z0-9]+(?:\|[^>]*)?>|[\w.'-]+(?:\s+[\w.'-]+)?)|\blet\s+(<@[A-Z0-9]+(?:\|[^>]*)?>|[\w.'-]+(?:\s+[\w.'-]+)?)\s+know\b`)
	incidentWebsiteDown = regexp.MustCompile(`(?i)\blive\s*-?\s*web\s*site\b.*\b(down|not working|broken|unreachable|offline)\b`)
	// yesReply and noReply are whole-message answers to an offer, so "yes,
	// but use port 9090" is a change rather than a yes.
	yesReply = regexp.MustCompile(`(?i)^\W*(yes|yep|yeah|sure|ok|okay|go ahead|do it|approved?|lgtm|sounds good|start it)(\W+(please|thanks|thank you|go ahead|do it))*\W*$`)
	noReply  = regexp.MustCompile(`(?i)^\W*(no|nope|cancel|stop|never ?mind|don't|do not|leave it)(\W+(thanks|thank you))*\W*$`)
)

const (
	// incidentHostLabel names the server an app runs on.
	incidentHostLabel = "hosted-on"
	// defaultIncidentSSHLogin is the login used on that server.
	defaultIncidentSSHLogin = "root"
)

// incidentFix is the fix the bot offered in a thread, waiting for the user's yes.
type incidentFix struct {
	// Beam is where the bot runs tsh.
	Beam  string `json:"beam"`
	App   string `json:"app"`
	Host  string `json:"host"`
	Login string `json:"login"`
	URI   string `json:"uri"`
	// Status is what systemctl reported for nginx when the site was down.
	Status string `json:"status"`
	// Incident is the reference number posted in the thread.
	Incident int `json:"incident"`
}

func incidentFixPath(threadPath string) string {
	return threadPath + ".incident-fix"
}

// incidentOwnerPath records the service owner told about the issue in a thread,
// so they hear when it's resolved.
func incidentOwnerPath(threadPath string) string {
	return threadPath + ".incident-owner"
}

const (
	incidentIssueMessage    = "There's an issue with the live website."
	incidentResolvedMessage = "The issue was resolved."
)

// runIncident handles the scripted incident requests. ok is false when the request
// isn't one of them.
// beams and threadBeam choose the beam the website check runs from.
func (s beamsSession) runIncident(ctx context.Context, threadPath, text string, beams []map[string]any, threadBeam string) (reply string, ok bool, err error) {
	if !s.runner.Config.IncidentResponse {
		return "", false, nil
	}
	// An offer to fix the website is answered by the next message.
	if data, err := os.ReadFile(incidentFixPath(threadPath)); err == nil {
		os.Remove(incidentFixPath(threadPath))
		var fix incidentFix
		if err := json.Unmarshal(data, &fix); err == nil {
			switch {
			case yesReply.MatchString(text):
				s.logRequest(ctx, "incident", text)
				reply, fixed, err := s.incidentStartNginx(ctx, fix)
				if err != nil {
					return "", true, trace.Wrap(err)
				}
				if fixed {
					if note := s.incidentTellOwnerResolved(ctx, threadPath, fix); note != "" {
						reply += "\n\n" + note
					}
				}
				return reply, true, nil
			case noReply.MatchString(text):
				return "OK, I'll leave nginx stopped.", true, nil
			}
		}
	}

	var parts []string
	target, owner, notify := incidentNotifyTarget(text)
	switch {
	case incidentWebsiteDown.MatchString(text):
		// The whole incident response: tell the service owner (or whoever
		// the request names), number the incident, then check the website.
		parts = append(parts, s.incidentTellOwner(ctx, threadPath, target))
		incident := 40000 + rand.IntN(60000)
		parts = append(parts, fmt.Sprintf("I'm tracking this as incident #%d.", incident))
		found, err := s.incidentCheckWebsite(ctx, threadPath, beams, threadBeam, text, incident)
		if err != nil {
			return "", true, trace.Wrap(err)
		}
		parts = append(parts, found)
	default:
		// Other "tell" requests are for the bot's usual messaging.
		if notify && owner {
			parts = append(parts, s.incidentTellOwner(ctx, threadPath, target))
		}
	}
	if len(parts) == 0 {
		return "", false, nil
	}
	s.logRequest(ctx, "incident", text)
	return strings.Join(parts, "\n\n"), true, nil
}

// incidentCheckWebsite finds the app with a hosted-on label and checks nginx on
// that server. If nginx is down, it offers to start it.
func (s beamsSession) incidentCheckWebsite(ctx context.Context, threadPath string, beams []map[string]any, threadBeam, text string, incident int) (string, error) {
	beam, _ := chooseBeam(beams, threadBeam, text)
	if beam == "" {
		reportProgress(ctx, "Starting a beam to work from…")
		_, created, err := s.createBeam(ctx, nil)
		if err != nil {
			return "", trace.Wrap(err, "creating a beam")
		}
		beam = created
	}
	reportProgress(ctx, fmt.Sprintf("Looking for the live website in Teleport from your beam `%s`…", beam))
	out, err := s.incidentTSH(ctx, beam, "apps", "ls", "--format=json")
	if err != nil {
		return "", trace.Wrap(err, "listing Teleport apps")
	}
	var apps []struct {
		Metadata struct {
			Name   string            `json:"name"`
			Labels map[string]string `json:"labels"`
		} `json:"metadata"`
		Spec struct {
			URI string `json:"uri"`
		} `json:"spec"`
	}
	if err := json.Unmarshal([]byte(out), &apps); err != nil {
		return "", trace.Wrap(err, "reading Teleport apps")
	}
	var fix incidentFix
	for _, app := range apps {
		host := app.Metadata.Labels[incidentHostLabel]
		if host == "" {
			continue
		}
		// Prefer the app that looks like the live website.
		if fix.App == "" || strings.Contains(strings.ToLower(app.Metadata.Name), "live") {
			fix = incidentFix{App: app.Metadata.Name, Host: host, URI: app.Spec.URI}
		}
	}
	if fix.App == "" {
		return fmt.Sprintf("I couldn't find an app with a `%s` label in Teleport, so I don't know which server runs the website.", incidentHostLabel), nil
	}
	fix.Beam = beam
	fix.Login = cmp.Or(s.runner.Config.IncidentSSHLogin, defaultIncidentSSHLogin)
	if err := followThread(threadPath); err != nil {
		return "", trace.Wrap(err)
	}

	status, err := s.incidentNginxStatus(ctx, fix)
	if err != nil {
		return "", trace.Wrap(err, "checking nginx on %s", fix.Host)
	}
	found := fmt.Sprintf("Found the `%s` app in Teleport, labeled `%s: %s`. From your beam `%s`, I connected to `%s` through Teleport as `%s` and checked nginx: it's *%s*.",
		fix.App, incidentHostLabel, fix.Host, fix.Beam, fix.Host, fix.Login, status)
	if status == "active" {
		return found + fmt.Sprintf(" The site answers HTTP %s from the server, so nginx looks fine there.", s.incidentHTTPStatus(ctx, fix)), nil
	}

	fix.Status = status
	fix.Incident = incident
	data, err := json.Marshal(fix)
	if err != nil {
		return "", trace.Wrap(err)
	}
	if err := os.WriteFile(incidentFixPath(threadPath), data, 0o600); err != nil {
		return "", trace.Wrap(err)
	}
	return found + " That's why the website is down.\n\nWant me to start nginx and test the site? Reply *yes* in this thread.", nil
}

// incidentStartNginx starts nginx on the server and tests the site. fixed is
// true when nginx is running and the site answers.
func (s beamsSession) incidentStartNginx(ctx context.Context, fix incidentFix) (reply string, fixed bool, err error) {
	reportProgress(ctx, fmt.Sprintf("Starting nginx on `%s`…", fix.Host))
	if _, err := s.incidentSSH(ctx, fix, "systemctl start nginx"); err != nil {
		return "", false, trace.Wrap(err, "starting nginx on %s", fix.Host)
	}
	status, err := s.incidentNginxStatus(ctx, fix)
	if err != nil {
		return "", false, trace.Wrap(err, "checking nginx on %s", fix.Host)
	}
	code := s.incidentHTTPStatus(ctx, fix)
	if status == "active" && (strings.HasPrefix(code, "2") || strings.HasPrefix(code, "3")) {
		return fmt.Sprintf("Started nginx on `%s`, and it's *active*. I tested `%s` from the server and got HTTP %s, so the live website is back up.", fix.Host, fix.URI, code), true, nil
	}
	return fmt.Sprintf("I ran `systemctl start nginx` on `%s`, but nginx is *%s* and the site answered HTTP %s. It needs a closer look.", fix.Host, status, code), false, nil
}

// incidentNotTargets are words after "tell" that aren't part of a name.
var incidentNotTargets = map[string]bool{
	"that": true, "there": true, "the": true, "about": true, "it": true, "its": true, "it's": true, "is": true,
	"we": true, "we're": true, "our": true, "this": true, "and": true, "to": true, "know": true, "site": true,
	"website": true, "live": true, "livewebsite": true, "issue": true, "service": true, "app": true,
}

// incidentNotifyTarget finds who a request asks to tell about the issue. target
// is empty for the service owner ("the owner", "them").
func incidentNotifyTarget(text string) (target string, owner, ok bool) {
	m := incidentNotify.FindStringSubmatch(text)
	if m == nil {
		return "", false, false
	}
	target = cmp.Or(m[1], m[2])
	if mentionID.MatchString(target) {
		return target, false, true
	}
	words := strings.Fields(target)
	for _, word := range words {
		switch strings.ToLower(strings.Trim(word, ".,'")) {
		case "owner", "owners", "him", "her", "them":
			return "", true, true
		case "me", "myself", "us":
			return "", false, false
		}
	}
	if incidentNotTargets[strings.ToLower(words[0])] {
		return "", false, false
	}
	if len(words) > 1 && incidentNotTargets[strings.ToLower(words[1])] {
		words = words[:1]
	}
	return strings.Join(words, " "), false, true
}

// incidentTellOwner sends a direct message about the issue to the person the
// request names, or the service owner.
func (s beamsSession) incidentTellOwner(ctx context.Context, threadPath, target string) string {
	owner, note := s.incidentRecipient(ctx, target)
	if owner == nil {
		return note
	}
	if err := s.runner.Post(ctx, owner.ID, "", incidentIssueMessage); err != nil {
		return fmt.Sprintf("I couldn't message %s: %s", owner.label(), trace.UserMessage(err))
	}
	s.logRequest(ctx, "tell", incidentIssueMessage, "to_slack_user_id", owner.ID)
	data, err := json.Marshal(owner)
	if err == nil {
		err = os.WriteFile(incidentOwnerPath(threadPath), data, 0o600)
	}
	if err == nil {
		err = followThread(threadPath)
	}
	if err != nil {
		logger.Get(ctx).WarnContext(ctx, "Failed to remember the service owner for this thread", "error", err)
	}
	return "I contacted the service owner on your behalf."
}

// incidentTellOwnerResolved tells the owner told about the issue in this thread
// that it's fixed, and why the site was down.
func (s beamsSession) incidentTellOwnerResolved(ctx context.Context, threadPath string, fix incidentFix) string {
	data, err := os.ReadFile(incidentOwnerPath(threadPath))
	if err != nil {
		return ""
	}
	os.Remove(incidentOwnerPath(threadPath))
	var owner SlackPerson
	if err := json.Unmarshal(data, &owner); err != nil || owner.ID == "" {
		return ""
	}
	message := fmt.Sprintf("%s The live website was down because nginx had stopped on `%s` (it was *%s*). I started nginx, and the site answers again.",
		incidentResolvedMessage, fix.Host, cmp.Or(fix.Status, "not running"))
	if err := s.runner.Post(ctx, owner.ID, "", message); err != nil {
		return fmt.Sprintf("I couldn't tell %s it's resolved: %s", owner.label(), trace.UserMessage(err))
	}
	s.logRequest(ctx, "tell", message, "to_slack_user_id", owner.ID)
	return "Let the service owner know it's resolved, and why it was down."
}

// incidentRecipient finds who to message in Slack: the person named, or the
// service owner. When a name like "Alex" matches several people, the service
// owner wins if they're one of them.
func (s beamsSession) incidentRecipient(ctx context.Context, target string) (*SlackPerson, string) {
	ownerName := s.runner.Config.ServiceOwner
	switch {
	case target == "" && ownerName == "":
		return nil, "I don't know who owns the live website. Set `service_owner` to their Slack name."
	case s.runner.FindPeople == nil || s.runner.Post == nil:
		return nil, "I can't send Slack messages from here."
	}
	name := cmp.Or(target, ownerName)
	people, err := s.runner.FindPeople(ctx, name)
	if err != nil {
		return nil, fmt.Sprintf("I couldn't look up %s in Slack: %s", name, trace.UserMessage(err))
	}
	if len(people) > 1 && target != "" && ownerName != "" {
		if owners, err := s.runner.FindPeople(ctx, ownerName); err == nil && len(owners) == 1 {
			for _, p := range people {
				if p.ID == owners[0].ID {
					return &p, ""
				}
			}
		}
	}
	switch len(people) {
	case 0:
		return nil, fmt.Sprintf("I couldn't find anyone in Slack called %s, so I didn't message them.", name)
	case 1:
		return &people[0], ""
	}
	return nil, fmt.Sprintf("More than one person in Slack matches %s, so I didn't message anyone. Say their full name.", name)
}

// followThread makes the bot follow a thread, so a plain reply like "yes"
// reaches it without a mention.
func followThread(threadPath string) error {
	if _, err := os.Stat(threadPath); os.IsNotExist(err) {
		return trace.Wrap(os.WriteFile(threadPath, nil, 0o600))
	}
	return nil
}

// incidentTSH runs tsh inside the beam, where it is logged in as the user.
func (s beamsSession) incidentTSH(ctx context.Context, beam string, args ...string) (string, error) {
	out, err := s.userTSH(ctx, "beams", "exec", beam, shellQuoteJoin(append([]string{"tsh"}, args...)))
	return out, trace.Wrap(err)
}

// incidentSSH runs a command on the server from the beam, through Teleport.
func (s beamsSession) incidentSSH(ctx context.Context, fix incidentFix, command string) (string, error) {
	if fix.Login != "root" && strings.HasPrefix(command, "systemctl start") {
		command = "sudo -n " + command
	}
	out, err := s.incidentTSH(ctx, fix.Beam, "ssh", fix.Login+"@"+fix.Host, command)
	return strings.TrimSpace(out), trace.Wrap(err)
}

func (s beamsSession) incidentNginxStatus(ctx context.Context, fix incidentFix) (string, error) {
	// is-active exits non-zero for a stopped service, which isn't an error here.
	out, err := s.incidentSSH(ctx, fix, "systemctl is-active nginx || true")
	if err != nil {
		return "", trace.Wrap(err)
	}
	status, _, _ := strings.Cut(out, "\n")
	return cmp.Or(status, "unknown"), nil
}

func (s beamsSession) incidentHTTPStatus(ctx context.Context, fix incidentFix) string {
	out, err := s.incidentSSH(ctx, fix, "curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "+shellQuoteJoin([]string{fix.URI})+" || true")
	if err != nil || out == "" {
		return "000"
	}
	return out
}
