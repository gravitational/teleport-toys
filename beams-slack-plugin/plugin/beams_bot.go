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
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/gravitational/trace"

	"github.com/gravitational/teleport/integrations/lib/logger"
)

// botActionPrefix marks lines in Claude's reply that ask the plugin to act
// on the beam from outside, which Claude cannot do from inside the VM.
const botActionPrefix = "BEAMS_BOT_ACTION:"

var (
	sessionIDText = regexp.MustCompile(`[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}`)
	slackMention  = regexp.MustCompile(`<@[A-Z0-9]+(?:\|[^>]*)?>`)
	botPrefix     = regexp.MustCompile(`(?i)^\s*@?teleport\s+beams\s+bot\b[\s,:]*`)
	safeThreadKey = regexp.MustCompile(`^[A-Z0-9]+-[0-9.]+$`)

	// Questions about which beams the user has are answered from tsh beams ls
	// directly, without a Claude run. Requests that act on beams go to Claude.
	beamsListQuestion = regexp.MustCompile(`(?i)\b(list|show|what|which|how many|do i have)\b`)
	beamsPlural       = regexp.MustCompile(`(?i)\bbeams\b`)
	beamsActionWord   = regexp.MustCompile(`(?i)\b(create|make|build|new|add|publish|unpublish|deploy|run|install|write|delete|remove|rm|start|stop|fix|update|change|set up|setup)\b`)
)

// botRequest extracts the request from a Slack message addressed to the
// bot. Mentions, direct messages, and replies in a thread the user already
// has with the bot (followUp) always count; other channel messages count only
// when they start with "Teleport Beams Bot".
//
// Mentions of the bot are dropped, but mentions of other people stay so a
// request can say who to message. Without botUserID, all mentions are dropped.
func botRequest(event BeamsMessageEvent, followUp bool, botUserID string) (string, bool) {
	// Uploads arrive as "file_share" messages; other subtypes are edits,
	// joins, bot posts, and the like.
	if event.User == "" || event.BotID != "" || (event.Subtype != "" && event.Subtype != "file_share") {
		return "", false
	}
	text := strings.TrimSpace(slackMention.ReplaceAllStringFunc(event.Text, func(mention string) string {
		if id := mentionID.FindStringSubmatch(mention); botUserID != "" && id[1] != botUserID {
			return mention
		}
		return ""
	}))
	switch {
	case event.EventType == "app_mention", event.ChannelType == "im", followUp:
		text = botPrefix.ReplaceAllString(text, "")
	case botPrefix.MatchString(event.Text):
		text = botPrefix.ReplaceAllString(event.Text, "")
	default:
		return "", false
	}
	text = strings.TrimSpace(text)
	if text == "" && len(event.Files) > 0 {
		text = "Here's an attachment."
	}
	return text, text != ""
}

// ThreadUsesBeam reports whether a request in the thread will run in a beam
// rather than possibly getting a chat answer.
func (r BeamsCommandRunner) ThreadUsesBeam(teamID, userID, threadKey string) bool {
	if !r.chatEnabled() {
		return true
	}
	if !safeSlackID.MatchString(teamID) || !safeSlackID.MatchString(userID) || !safeThreadKey.MatchString(threadKey) {
		return false
	}
	return readThreadState(filepath.Join(r.Config.ProfilesDir, teamID, userID, "threads", threadKey)).beam != ""
}

type progressKey struct{}

// withProgress attaches a function that posts progress updates to the
// request's Slack thread.
func withProgress(ctx context.Context, post func(text string)) context.Context {
	return context.WithValue(ctx, progressKey{}, post)
}

// reportProgress posts a progress update, if the caller asked for them.
func reportProgress(ctx context.Context, text string) {
	if post, ok := ctx.Value(progressKey{}).(func(string)); ok {
		post(text)
	}
}

// FollowsThread reports whether a Slack user already has a bot
// conversation in the thread, so their replies there need no mention.
func (r BeamsCommandRunner) FollowsThread(teamID, userID, threadKey string) bool {
	if !safeSlackID.MatchString(teamID) || !safeSlackID.MatchString(userID) || !safeThreadKey.MatchString(threadKey) {
		return false
	}
	_, err := os.Stat(filepath.Join(r.Config.ProfilesDir, teamID, userID, "threads", threadKey))
	return err == nil
}

// beamsIntroQuestion matches questions about what Beams are or what you can
// do with them, answered with beamsIntro.
var beamsIntroQuestion = regexp.MustCompile(`(?i)^\W*(what\s+(can|could)\s+(i|we|you)\s+do\s+with\s+(teleport\s+)?beams?|what\s+(are|is)\s+(teleport\s+)?beams|what\s+is\s+a\s+beam|what\s+are\s+beams\s+for)\W*$`)

func isBeamsIntroQuestion(text string) bool {
	return beamsIntroQuestion.MatchString(strings.TrimSpace(text))
}

// beamsIntro summarizes https://goteleport.com/docs/beams/ in terms of what
// this Slack integration can do. The incident line appears only when incident
// response is on.
func beamsIntro(incidentResponse bool) string {
	var b strings.Builder
	b.WriteString(`I can get things done for you using Teleport, I work via short-lived computers in the cloud called Beams where AI agents can work safely and securely. Each beam belongs to you, everything done in it is recorded under your name, and it cleans itself up after a day.

Here's what you can do, right here in Slack:
`)
	if incidentResponse {
		b.WriteString(`• *Report a problem.* "@Teleport Beams Bot live website is down": I'll contact the service owner, diagnose the server behind the site through Teleport, and offer the fix.
`)
	}
	b.WriteString(`• *Get work done.* "@Teleport Beams Bot build a status page for our API and publish it." I'll show you my plan, and once you say yes, do the work in one of your beams and reply with a link.
• *Loop people in.* "tell Anna the website is down" and I'll message her right away.
• *Keep going in the same thread* to make changes, like "make the title bigger."
• *Manage your beams*: "how many beams do I have?", "create a new beam", or "delete this beam".

Comfortable with commands? ` + "`/beams help`" + ` lists everything, including running Claude Code or Codex yourself.
Learn more: https://goteleport.com/docs/beams/`)
	return b.String()
}

// isInstantRequest reports whether the bot answers a request immediately,
// without Teleport or a Claude run.
func isInstantRequest(text string) bool {
	return isBeamsIntroQuestion(text)
}

// Handle handles a natural-language request. It picks one of the user's beams
// (or creates one), hands the request to Claude Code there, and carries out
// the actions Claude asks for. Requests in the same Slack thread continue the
// same beam and Claude conversation.
func (r BeamsCommandRunner) Handle(ctx context.Context, teamID, userID, threadKey, text string) (AskResult, error) {
	if !safeThreadKey.MatchString(threadKey) {
		return AskResult{}, trace.BadParameter("invalid Slack thread")
	}
	if isBeamsIntroQuestion(text) {
		return AskResult{Reply: beamsIntro(r.Config.IncidentResponse)}, nil
	}
	s, err := r.newSession(ctx, teamID, userID)
	if err != nil {
		return AskResult{}, trace.Wrap(err)
	}

	threadsDir := filepath.Join(s.profileDir, "threads")
	if err := os.MkdirAll(threadsDir, 0o700); err != nil {
		return AskResult{}, trace.Wrap(err)
	}
	channel, threadTS, _ := strings.Cut(threadKey, "-")

	// The bot only works as the user, so beams it creates are owned by them and
	// their published URLs are reachable. Until the user authorizes the bot,
	// hold the request and explain how in a direct message.
	var notes []string
	if s.delegation == "" {
		id := sessionIDText.FindString(text)
		if id == "" {
			return s.requestAuthorization(channel, threadTS, text)
		}
		if _, err := s.connect(ctx, []string{id}); err != nil {
			return AskResult{}, trace.Wrap(err, "connecting delegation session")
		}
		s.delegation = strings.ToLower(id)
		connected := fmt.Sprintf("Thanks, you're connected as %s.", s.username)
		pending := s.takePendingRequest()
		switch {
		case pending == nil:
			return AskResult{Reply: connected + " What would you like me to do?"}, nil
		case pending.Channel != channel || pending.ThreadTS != threadTS:
			// Continue the request in the thread where it was asked.
			return AskResult{Reply: connected + " I'll pick up your request where you asked it.", Resume: pending}, nil
		}
		notes = append(notes, connected)
		text = pending.Text
	}

	beams, err := s.delegatedBeams(ctx)
	if needsAuthorization(err) {
		return s.requestAuthorization(channel, threadTS, text)
	}
	if err != nil {
		return AskResult{}, trace.Wrap(err)
	}
	threadPath := filepath.Join(threadsDir, threadKey)
	thread := readThreadState(threadPath)
	if reply, ok, err := s.runIncident(ctx, threadPath, text, beams, thread.beam); ok || err != nil {
		return AskResult{Reply: truncateSlackOutput(strings.Join(append(notes, reply), "\n\n"))}, trace.Wrap(err)
	}
	// A plan waiting for approval is answered by the next message: yes carries
	// it out, no drops it, and anything else revises it.
	awaitingApproval := takeApproval(threadPath)
	if awaitingApproval && noReply.MatchString(text) {
		return AskResult{Reply: "OK, I won't go ahead with that."}, nil
	}
	if names, ok := removeTargets(text, beams, thread.beam); ok {
		s.logRequest(ctx, "delete", text, "beams", names)
		reply, err := s.removeBeams(ctx, beams, names, thread, threadPath, notes)
		return AskResult{Reply: reply}, trace.Wrap(err)
	}
	if isBeamsListQuestion(text) {
		s.logRequest(ctx, "list", text)
		threadPath := filepath.Join(threadsDir, threadKey)
		if _, err := os.Stat(threadPath); os.IsNotExist(err) {
			if err := os.WriteFile(threadPath, nil, 0o600); err != nil {
				return AskResult{}, trace.Wrap(err)
			}
		}
		return AskResult{Reply: truncateSlackOutput(strings.Join(append(notes, formatBeamsList(beams, time.Now(), "You don't have any beams yet. Ask me to create one.")), "\n\n"))}, nil
	}

	agent := thread.agent
	if requested := requestedAgent(text); requested != "" && requested != agent {
		agent = requested
		notes = append(notes, fmt.Sprintf("Using %s for this thread.", agentDisplayName(agent)))
	}
	// Threads that haven't started work in a beam are answered by the model
	// directly through Teleport's LLM app, as the user: quick, no beam, and
	// audited. The model hands off requests that need a computer.
	var chatHistory []chatMessage
	// told tracks the Slack messages sent for the user during this request.
	told := map[string]string{}
	files := attachmentsFrom(ctx)
	images, imagesFit := chatImages(files)
	if thread.beam == "" && r.chatEnabled() && !namesBeam(beams, text) && imagesFit {
		reply, handoff, history, actions, err := s.chatTurn(ctx, chatHistoryPath(threadsDir, threadKey), agent, text, images)
		switch {
		case needsAuthorization(err):
			return s.requestAuthorization(channel, threadTS, text)
		case trace.IsNotFound(err) || trace.IsNotImplemented(err):
			// No LLM apps on this tenant: every request goes to a beam.
			logger.Get(ctx).WarnContext(ctx, "Chat is unavailable, using a coding agent in a beam instead", "error", err)
		case err != nil:
			return AskResult{}, trace.Wrap(err, "getting a chat answer from %s", agentDisplayName(agent))
		default:
			// Messages for other people go out right away, before any beam work.
			var tellNotes []string
			for _, action := range actions {
				if note := s.tell(ctx, channel, threadTS, action, told); note != "" {
					tellNotes = append(tellNotes, note)
				}
			}
			if !handoff {
				if err := writeThreadState(threadPath, threadState{agent: agent}); err != nil {
					return AskResult{}, trace.Wrap(err)
				}
				if beam, _ := chooseBeam(beams, "", ""); beam != "" {
					// After the answer is posted, so chat stays fast.
					record := func() { s.recordChatInBeam(context.WithoutCancel(ctx), beam, agent, text, reply) }
					if s.runner.Background != nil {
						s.runner.Background(record)
					} else {
						go record()
					}
				}
				parts := append(append(notes, reply), tellNotes...)
				return AskResult{Reply: truncateSlackOutput(strings.Join(parts, "\n\n"))}, nil
			}
			chatHistory = history
			for _, note := range tellNotes {
				reportProgress(ctx, note)
			}
		}
	}

	// The request is going to a beam. If it started as a possible chat
	// question, the user was told "Just a moment", so say what's happening.
	if thread.beam == "" && r.chatEnabled() {
		reportProgress(ctx, "This needs a computer, so I'm working in one of your beams. This can take a few minutes.")
	}

	name, continued := chooseBeam(beams, thread.beam, text)
	// Only continue a conversation the same agent already has in this beam.
	if name != thread.beam || thread.fresh || agent != thread.agent {
		continued = false
	}

	created := false
	if name == "" {
		_, name, err = s.createBeam(ctx, nil)
		if err != nil {
			return AskResult{}, trace.Wrap(err, "creating a beam")
		}
		created = true
		notes = append(notes, fmt.Sprintf("Created beam `%s`.", name))
	}
	// Mark the beam fresh until an agent run succeeds there, so a failed first
	// run is not followed by --continue.
	if err := writeThreadState(threadPath, threadState{beam: name, fresh: !continued, agent: agent}); err != nil {
		return AskResult{}, trace.Wrap(err)
	}

	// Copy attachments into the beam for the agent.
	request := text
	if len(files) > 0 {
		paths, err := s.uploadAttachments(ctx, name, files)
		if err != nil {
			return AskResult{}, trace.Wrap(err)
		}
		request = text + "\n\n" + attachmentPrompt(paths)
	}
	if sent := alreadyTold(told); sent != "" {
		request += "\n\n" + sent
	}

	// Claude plans first and acts once the user approves the plan, which only
	// makes sense in the conversation that made it.
	approved := awaitingApproval && continued && yesReply.MatchString(text)
	planning := agent == agentClaude && !r.Config.ClaudeSkipApproval && !approved
	prompt := botPrompt(name, s.username, created, planning, chatContext(chatHistory), request)
	switch {
	case approved:
		prompt = "The user approved your plan. Carry it out now, then reply with the result."
	case awaitingApproval && continued:
		prompt = "The user replied to your plan: " + request + "\nRevise the plan, or answer them if nothing needs to change."
	}

	s.logRequest(ctx, "beam", text,
		"beam", name, "agent", agent, "continued", continued, "planning", planning, "approved", approved,
		"attachments", attachmentNames(files))

	agentArgs := []string{name}
	if continued {
		agentArgs = append(agentArgs, "--continue")
	}
	agentArgs = append(agentArgs, prompt)
	run := func(ctx context.Context, args []string) (string, error) {
		return s.claudeForBot(ctx, args, planning)
	}
	if agent == agentCodex {
		run = s.codex
	}
	output, err := run(ctx, agentArgs)
	if err != nil {
		return AskResult{}, trace.Wrap(err, "running %s in beam %q", agentDisplayName(agent), name)
	}
	if err := writeThreadState(threadPath, threadState{beam: name, agent: agent}); err != nil {
		return AskResult{}, trace.Wrap(err)
	}

	reply, actions := parseBotActions(output)
	var actionNotes []string
	for _, action := range actions {
		if action == "approve" {
			if planning {
				if err := os.WriteFile(approvalPath(threadPath), nil, 0o600); err != nil {
					return AskResult{}, trace.Wrap(err)
				}
				actionNotes = append(actionNotes, "Reply *yes* to go ahead, or tell me what to change.")
			}
			continue
		}
		if _, _, ok := parseTell(action); ok {
			if note := s.tell(ctx, channel, threadTS, action, told); note != "" {
				actionNotes = append(actionNotes, note)
			}
			continue
		}
		if planning {
			// Nothing changes until the plan is approved.
			continue
		}
		note, newBeam, err := s.botAction(ctx, name, action)
		if err != nil {
			note = fmt.Sprintf("Could not %s: %s", action, trace.UserMessage(err))
		}
		actionNotes = append(actionNotes, note)
		if newBeam != "" {
			// Follow-ups in this thread should work in the beam the user asked
			// for, starting a new agent conversation there.
			name = newBeam
			if err := writeThreadState(threadPath, threadState{beam: name, fresh: true, agent: agent}); err != nil {
				return AskResult{}, trace.Wrap(err)
			}
		}
	}

	parts := append(notes, reply)
	parts = append(parts, actionNotes...)
	return AskResult{Reply: truncateSlackOutput(strings.TrimSpace(strings.Join(parts, "\n\n")))}, nil
}

const (
	agentClaude = "claude"
	agentCodex  = "codex"
)

// agentRequest matches a request to use a particular coding agent, such as
// "use codex to ..." or a request that starts with "codex".
var agentRequest = regexp.MustCompile(`(?i)(?:^\W*|\b(?:use|using|with|via|through|ask)\s+)(claude(?:\s+code)?|codex)\b`)

// requestedAgent returns the agent a request asks for, or "" for none.
func requestedAgent(text string) string {
	m := agentRequest.FindStringSubmatch(text)
	if m == nil {
		return ""
	}
	if strings.HasPrefix(strings.ToLower(m[1]), agentCodex) {
		return agentCodex
	}
	return agentClaude
}

func agentDisplayName(agent string) string {
	if agent == agentCodex {
		return "Codex"
	}
	return "Claude Code"
}

// threadState is what the bot remembers about a Slack thread: its beam, which
// agent it uses, and whether that agent has no conversation there yet.
type threadState struct {
	beam  string
	fresh bool
	agent string
}

// readThreadState parses "<beam> [new] [claude|codex]", where beam "-" means
// a chat thread with no beam yet. Older files without an agent used Claude.
func readThreadState(path string) threadState {
	state := threadState{agent: agentClaude}
	data, err := os.ReadFile(path)
	if err != nil {
		return state
	}
	fields := strings.Fields(string(data))
	if len(fields) > 0 && fields[0] != "-" {
		state.beam = fields[0]
	}
	for _, field := range fields[min(1, len(fields)):] {
		switch field {
		case "new":
			state.fresh = true
		case agentClaude, agentCodex:
			state.agent = field
		}
	}
	return state
}

func writeThreadState(path string, state threadState) error {
	// A thread with no beam yet keeps its file, so replies still count as
	// follow-ups, and remembers its agent.
	fields := []string{cmp.Or(state.beam, "-")}
	if state.fresh {
		fields = append(fields, "new")
	}
	fields = append(fields, cmp.Or(state.agent, agentClaude))
	return trace.Wrap(os.WriteFile(path, []byte(strings.Join(fields, " ")), 0o600))
}

// removeVerb starts a request to delete beams.
var removeVerb = regexp.MustCompile(`(?i)^\W*(?:please\s+)?(?:delete|remove|destroy|rm|tear\s+down|get\s+rid\s+of)\b`)

// removeTargets reports whether a request asks to delete beams and which.
// The beams must follow the verb directly ("delete curious-shield", "remove
// beam a and b", "delete this beam"), so requests like "remove the header
// from the page in curious-shield" still go to the coding agent. "this beam"
// or "it" means the thread's beam. ok with no names means the user asked to
// delete beams without saying which.
func removeTargets(text string, beams []map[string]any, threadBeam string) (names []string, ok bool) {
	loc := removeVerb.FindStringIndex(text)
	if loc == nil {
		return nil, false
	}
	known := map[string]bool{}
	var all []string
	for _, beam := range beams {
		if id, _ := beam["id"].(string); id != "" {
			known[id] = true
			all = append(all, id)
		}
	}
	thisBeam, beamWord, allWord := false, false, false
	for _, word := range strings.Fields(text[loc[1]:]) {
		word = strings.Trim(word, ",.!?;:`'\"*_")
		switch lower := strings.ToLower(word); {
		case known[word]:
			names = append(names, word)
		case lower == "this" || lower == "that" || lower == "it":
			thisBeam = true
		case lower == "beam":
			beamWord = true
			thisBeam = true
		case lower == "beams":
			beamWord = true
		case lower == "all" || lower == "every":
			allWord = true
		case lower == "the" || lower == "my" || lower == "of" || lower == "and" || lower == "both" || lower == "please" || lower == "":
		default:
			if len(names) == 0 && !beamWord && !thisBeam {
				return nil, false
			}
		}
	}
	// "delete all my beams" deletes every beam the user owns.
	if len(names) == 0 && allWord && beamWord {
		return all, true
	}
	if len(names) == 0 && thisBeam && threadBeam != "" && known[threadBeam] {
		names = []string{threadBeam}
	}
	return names, len(names) > 0 || beamWord || thisBeam
}

// removeBeams deletes the named beams as the user and forgets the thread's
// beam if it was one of them.
func (s beamsSession) removeBeams(ctx context.Context, beams []map[string]any, names []string, thread threadState, threadPath string, notes []string) (string, error) {
	if len(names) == 0 {
		notes = append(notes, "Which beam should I delete? Say \"delete\" and its name.\n\n"+
			formatBeamsList(beams, time.Now(), "You don't have any beams."))
		return truncateSlackOutput(strings.Join(notes, "\n\n")), nil
	}
	for _, name := range names {
		if _, err := s.target(ctx, []string{"rm", name}); err != nil {
			notes = append(notes, fmt.Sprintf("Could not delete `%s`: %s", name, trace.UserMessage(err)))
			continue
		}
		notes = append(notes, fmt.Sprintf("Deleted beam `%s`.", name))
		if name == thread.beam {
			thread.beam, thread.fresh = "", false
		}
	}
	if err := writeThreadState(threadPath, thread); err != nil {
		return "", trace.Wrap(err)
	}
	return truncateSlackOutput(strings.Join(notes, "\n\n")), nil
}

// AskResult is the bot's response to a Slack message.
type AskResult struct {
	// Reply is posted in the thread the message came from.
	Reply string
	// DM, if set, is sent to the user as a direct message. Authorization
	// instructions go here so they are not posted in a shared channel.
	DM string
	// Resume, if set, is a request to run again in its original thread, now
	// that the user has authorized the bot from a direct message.
	Resume *PendingRequest
}

// PendingRequest is a request held until the user authorizes the bot.
type PendingRequest struct {
	Channel  string `json:"channel"`
	ThreadTS string `json:"thread_ts"`
	Text     string `json:"text"`
}

// isDirectMessage reports whether a Slack channel ID is a direct message.
func isDirectMessage(channel string) bool {
	return strings.HasPrefix(channel, "D")
}

func (s beamsSession) pendingPath() string {
	return filepath.Join(s.profileDir, "pending-request")
}

// takePendingRequest returns and forgets the request held for authorization.
func (s beamsSession) takePendingRequest() *PendingRequest {
	data, err := os.ReadFile(s.pendingPath())
	if err != nil {
		return nil
	}
	_ = os.Remove(s.pendingPath())
	var pending PendingRequest
	if json.Unmarshal(data, &pending) != nil || pending.Text == "" {
		return nil
	}
	return &pending
}

// requestAuthorization holds the request and asks the user to let the bot
// create beams as them. The instructions always go to a direct message; a
// request made in a channel only gets a pointer to it there.
func (s beamsSession) requestAuthorization(channel, threadTS, request string) (AskResult, error) {
	if s.runner.Config.BotName == "" {
		return AskResult{}, trace.BadParameter("delegation is not configured; set beams.bot_name")
	}
	data, err := json.Marshal(PendingRequest{Channel: channel, ThreadTS: threadTS, Text: request})
	if err != nil {
		return AskResult{}, trace.Wrap(err)
	}
	if err := os.WriteFile(s.pendingPath(), data, 0o600); err != nil {
		return AskResult{}, trace.Wrap(err)
	}
	if isDirectMessage(channel) {
		return AskResult{Reply: s.authorizationReply("Reply here with the session ID it prints and I'll pick up your request.")}, nil
	}
	return AskResult{
		Reply: "To keep your credentials private, I've sent you a direct message to set up access. I'll come back to this thread once you're connected.",
		DM:    s.authorizationReply("Reply to me here with the session ID it prints and I'll pick up your request in its thread."),
	}, nil
}

// Connected reports whether a Slack user has a delegation session on file. It
// does not check that the session is still valid.
func (r BeamsCommandRunner) Connected(teamID, userID string) bool {
	if !safeSlackID.MatchString(teamID) || !safeSlackID.MatchString(userID) {
		return false
	}
	_, err := os.Stat(filepath.Join(r.Config.ProfilesDir, teamID, userID, "delegation-session"))
	return err == nil
}

// isBeamsListQuestion reports whether a request only asks which beams the
// user has.
func isBeamsListQuestion(text string) bool {
	return beamsPlural.MatchString(text) && beamsListQuestion.MatchString(text) && !beamsActionWord.MatchString(text)
}

// formatBeamsList describes the user's beams for Slack. empty is the reply
// when there are none.
func formatBeamsList(beams []map[string]any, now time.Time, empty string) string {
	if len(beams) == 0 {
		return empty
	}
	var b strings.Builder
	if len(beams) == 1 {
		b.WriteString("You have *1 beam*:")
	} else {
		fmt.Fprintf(&b, "You have *%d beams*:", len(beams))
	}
	for _, beam := range beams {
		id, _ := beam["id"].(string)
		fmt.Fprintf(&b, "\n• `%s`", id)
		if region, _ := beam["region"].(string); region != "" {
			fmt.Fprintf(&b, " (%s)", region)
		}
		if expires, _ := beam["expires"].(string); expires != "" {
			if t, err := time.Parse(time.RFC3339Nano, expires); err == nil {
				fmt.Fprintf(&b, ", expires in %s", humanDuration(t.Sub(now)))
			}
		}
		if url, _ := beam["url"].(string); url != "" {
			fmt.Fprintf(&b, ", published at %s", url)
		}
	}
	return b.String()
}

func humanDuration(d time.Duration) string {
	switch {
	case d < time.Minute:
		return "less than a minute"
	case d < time.Hour:
		return plural(int(d.Round(time.Minute)/time.Minute), "minute")
	default:
		return plural(int(d.Round(time.Hour)/time.Hour), "hour")
	}
}

func plural(n int, unit string) string {
	if n == 1 {
		return "1 " + unit
	}
	return fmt.Sprintf("%d %ss", n, unit)
}

// namesBeam reports whether text mentions one of the user's beams by name.
func namesBeam(beams []map[string]any, text string) bool {
	for _, beam := range beams {
		if id, _ := beam["id"].(string); id != "" && strings.Contains(text, id) {
			return true
		}
	}
	return false
}

// chooseBeam picks the beam for a request: the thread's beam, a beam named in
// the request, or the user's newest beam. continued reports whether the
// thread already has a Claude conversation in that beam.
func chooseBeam(beams []map[string]any, threadBeam, text string) (name string, continued bool) {
	var newest, newestExpiry string
	for _, beam := range beams {
		id, _ := beam["id"].(string)
		if id == "" {
			continue
		}
		if id == threadBeam {
			return id, true
		}
		if strings.Contains(text, id) {
			name = id
		}
		if expires, _ := beam["expires"].(string); newest == "" || expires > newestExpiry {
			newest, newestExpiry = id, expires
		}
	}
	if name != "" {
		return name, false
	}
	return newest, false
}

// approvalPath marks a thread whose plan is waiting for the user's approval.
func approvalPath(threadPath string) string {
	return threadPath + ".approval"
}

// takeApproval reports whether the thread has a plan waiting for approval,
// clearing it: the user's next message answers it.
func takeApproval(threadPath string) bool {
	return os.Remove(approvalPath(threadPath)) == nil
}

// planInstructions tell Claude, in plan mode, to answer or propose a plan.
const planInstructions = `You are in plan mode: you can look around with read-only tools but cannot change anything yet. If the request only needs information, answer it. If it needs changes (writing or editing files, running commands that change things, installing software, starting services, publishing), reply with a short plan instead: what you'll do in a few bullets, including any script you'll write. Then end with the line:
` + botActionPrefix + ` approve
The Slack bot asks the user to approve, and you'll carry the plan out in this same conversation. Until then, don't request publish, unpublish, or create_beam. Don't mention plan mode, plan files, or ExitPlanMode.
`

func botPrompt(beam, username string, created, planning bool, earlier, request string) string {
	var b strings.Builder
	fmt.Fprintf(&b, "You are the Teleport Beams Bot, a Slack assistant working inside the Teleport Beam %q, an ephemeral Debian runtime, on behalf of %s.", beam, username)
	if created {
		b.WriteString(" This beam was just created for this request.")
	}
	b.WriteString(`
Carry out the request below in this runtime. Your final reply is posted to Slack, so keep it short and use Slack formatting. Reply to the user directly: do not restate their request or explain these instructions. If you mention this environment, call it a runtime, not a sandbox.
To share a web page or service, serve it on port 8080 on 0.0.0.0 in the background so it keeps running after you exit (for example with nohup or tmux), then request publishing.
You cannot manage beams yourself. To have the Slack bot act for you, end your reply with one line per action:
` + botActionPrefix + ` publish
` + botActionPrefix + ` unpublish
` + botActionPrefix + ` create_beam
Only request create_beam if the user explicitly asks for an additional beam.
` + tellInstructions + `
The Slack bot deletes beams itself: if the user asks to delete beams, tell them to say "delete" followed by the beam names, or "delete all my beams".
`)
	if planning {
		b.WriteString(planInstructions)
	}
	b.WriteString("\n")
	if earlier != "" {
		b.WriteString("Earlier in this Slack conversation:\n")
		b.WriteString(earlier)
		b.WriteString("\n")
	}
	b.WriteString("Request: ")
	b.WriteString(request)
	return b.String()
}

// parseBotActions separates action lines from Claude's reply.
func parseBotActions(output string) (string, []string) {
	var reply []string
	var actions []string
	for _, line := range strings.Split(output, "\n") {
		if action, ok := strings.CutPrefix(strings.TrimSpace(line), botActionPrefix); ok {
			if action = strings.TrimSpace(action); action != "" {
				actions = append(actions, action)
			}
			continue
		}
		reply = append(reply, line)
	}
	return strings.TrimSpace(strings.Join(reply, "\n")), actions
}

// botAction runs one action Claude requested. newBeam is set when the
// action created a beam.
func (s beamsSession) botAction(ctx context.Context, beam, action string) (note, newBeam string, err error) {
	switch action {
	case "publish":
		out, err := s.target(ctx, []string{"publish", beam, "--format=json"})
		if err != nil {
			return "", "", trace.Wrap(err)
		}
		var published struct {
			URL string `json:"url"`
		}
		if json.Unmarshal([]byte(out), &published) == nil && published.URL != "" {
			return fmt.Sprintf("Published: %s", published.URL), "", nil
		}
		return fmt.Sprintf("Published `%s`.", beam), "", nil
	case "unpublish":
		if _, err := s.target(ctx, []string{"unpublish", beam}); err != nil {
			return "", "", trace.Wrap(err)
		}
		return fmt.Sprintf("Unpublished `%s`.", beam), "", nil
	case "create_beam":
		_, name, err := s.createBeam(ctx, nil)
		if err != nil {
			return "", "", trace.Wrap(err)
		}
		return fmt.Sprintf("Created beam `%s`. This thread now works in it.", name), name, nil
	default:
		return "", "", trace.BadParameter("unsupported action %q", action)
	}
}
