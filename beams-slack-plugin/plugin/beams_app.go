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
	"strings"
	"sync"

	"github.com/gravitational/trace"

	delegationv1 "github.com/gravitational/teleport/api/gen/proto/go/teleport/delegation/v1"
	"github.com/gravitational/teleport/integrations/access/common"
	"github.com/gravitational/teleport/integrations/lib"
	"github.com/gravitational/teleport/integrations/lib/logger"
)

// BeamsBot defines the Slack operations used by the Beams slash-command app.
type BeamsBot interface {
	GenerateWebSocketURL(ctx context.Context) (string, error)
	PostBeamsReply(ctx context.Context, responseURL, text string) error
	LookupEmailByUserID(ctx context.Context, userID string) (string, error)
	PostBeamsThreadReply(ctx context.Context, channelID, threadTS, text string) error
	TeleportClusterName() string
	DownloadSlackFile(ctx context.Context, fileURL string, limit int64) ([]byte, error)
	BotUserID(ctx context.Context) (string, error)
	FindSlackPeople(ctx context.Context, query string) ([]SlackPerson, error)
}

type beamsRunner interface {
	Run(ctx context.Context, teamID, userID, text string) (string, error)
	Handle(ctx context.Context, teamID, userID, threadKey, text string) (AskResult, error)
	FollowsThread(teamID, userID, threadKey string) bool
	Connected(teamID, userID string) bool
	ThreadUsesBeam(teamID, userID, threadKey string) bool
}

// BeamsApp consumes /beams commands from Slack Socket Mode.
type BeamsApp struct {
	conf             BeamsConfig
	bot              BeamsBot
	runner           beamsRunner
	socketModeClient *SocketModeClient
	job              lib.ServiceJob
	// seen drops duplicate deliveries of the same Slack message.
	seen *recentSet
	// threads runs one bot request at a time per Slack thread.
	threads *threadQueue
	// botUserID is the bot's Slack user, whose mentions are dropped from
	// requests.
	botUserID string
}

// NewBeamsApp creates a Beams slash-command application.
func NewBeamsApp(conf BeamsConfig) *BeamsApp {
	app := &BeamsApp{
		conf:    conf,
		seen:    newRecentSet(1000),
		threads: newThreadQueue(),
	}
	app.job = lib.NewServiceJob(app.run)
	return app
}

// Init initializes the Slack bot and Socket Mode client.
func (a *BeamsApp) Init(baseApp *common.BaseApp) error {
	var ok bool
	a.bot, ok = baseApp.Bot.(BeamsBot)
	if !ok {
		return trace.BadParameter("bot does not support Beams slash commands")
	}
	a.socketModeClient = NewSocketModeClient(a.bot)
	delegationClient, ok := baseApp.APIClient.(interface {
		DelegationSessionServiceClient() delegationv1.DelegationSessionServiceClient
	})
	if !ok {
		return trace.BadParameter("Teleport client does not support delegation sessions")
	}
	a.runner = BeamsCommandRunner{
		Config:           a.conf,
		DelegationClient: delegationClient.DelegationSessionServiceClient(),
		LookupEmail:      a.bot.LookupEmailByUserID,
		Users:            baseApp.APIClient,
		Apps:             baseApp.APIClient,
		ClusterName:      a.bot.TeleportClusterName(),
		FindPeople:       a.bot.FindSlackPeople,
		Post:             a.bot.PostBeamsThreadReply,
	}
	return nil
}

// Start starts the application.
func (a *BeamsApp) Start(process *lib.Process) {
	process.SpawnCriticalJob(a.job)
}

// WaitReady waits until the application is ready.
func (a *BeamsApp) WaitReady(ctx context.Context) (bool, error) {
	return a.job.WaitReady(ctx)
}

// WaitForDone waits for the application to stop.
func (a *BeamsApp) WaitForDone() {
	<-a.job.Done()
}

// Err returns the application error.
func (a *BeamsApp) Err() error {
	return a.job.Err()
}

func (a *BeamsApp) run(ctx context.Context) error {
	process := lib.MustGetProcess(ctx)
	ctx, cancel := context.WithCancel(ctx)
	process.OnTerminate(func(context.Context) error {
		cancel()
		return nil
	})
	if id, err := a.bot.BotUserID(ctx); err != nil {
		logger.Get(ctx).WarnContext(ctx, "Failed to look up the bot's Slack user, so requests can't mention people", "error", err)
	} else {
		a.botUserID = id
	}

	log := logger.Get(ctx)
	a.job.SetReady(true)
	log.InfoContext(ctx, "Beams slash-command app is running")

	go func() {
		for {
			select {
			case interaction := <-a.socketModeClient.Interactions():
				switch event := interaction.(type) {
				case SlashCommandEvent:
					if event.Command == "/beams" {
						go a.handleSlashCommand(ctx, event)
					}
				case BeamsMessageEvent:
					followUp := event.EventType == "message" && event.ThreadTS != "" &&
						a.runner.FollowsThread(event.TeamID, event.User, event.Channel+"-"+event.ThreadTS)
					if request, ok := botRequest(event, followUp, a.botUserID); ok && a.seen.add(event.Channel+"-"+event.TS) {
						go a.handleMessage(ctx, event, request)
					}
				}
			case <-ctx.Done():
				return
			}
		}
	}()

	return trace.Wrap(a.socketModeClient.Run(ctx))
}

func (a *BeamsApp) handleSlashCommand(ctx context.Context, event SlashCommandEvent) {
	log := logger.Get(ctx).With(
		"slack_team_id", event.TeamID,
		"slack_user_id", event.UserID,
		"slack_channel_id", event.ChannelID,
	)
	log.InfoContext(ctx, "Executing Beams slash command")

	agentNames := map[string]string{"claude": "Claude Code", "codex": "Codex"}
	if fields := strings.Fields(event.Text); len(fields) > 1 && agentNames[fields[0]] != "" {
		notice := fmt.Sprintf("Running %s in `%s`. This can take a few minutes.", agentNames[fields[0]], fields[1])
		if err := a.bot.PostBeamsReply(ctx, event.ResponseURL, notice); err != nil {
			log.WarnContext(ctx, "Failed to post Beams progress notice", "error", err)
		}
	}

	output, err := a.runner.Run(ctx, event.TeamID, event.UserID, event.Text)
	if err != nil {
		output = fmt.Sprintf("Beams command failed: %s", trace.UserMessage(err))
		log.WarnContext(ctx, "Beams slash command failed", "error", err)
	}

	if err := a.bot.PostBeamsReply(ctx, event.ResponseURL, strings.TrimSpace(output)); err != nil {
		log.ErrorContext(ctx, "Failed to post Beams command response", "error", err)
	}
}

// beamWorkReply acknowledges a request that runs in a beam.
const beamWorkReply = "On it. Working in your beam; this can take a few minutes."

// Busy-thread notice for messages that arrive while the bot is still working.
const botBusyReply = "Still working on your earlier request; I'll pick this up when it's done."

func (a *BeamsApp) handleMessage(ctx context.Context, event BeamsMessageEvent, request string) {
	threadTS := cmp.Or(event.ThreadTS, event.TS)
	key := event.Channel + "-" + threadTS
	// Only one Claude run per thread: concurrent runs in the same beam would
	// each start without the other's context. Messages that arrive meanwhile
	// are answered right away and the newest one runs next.
	if !a.threads.start(key, queuedMessage{event: event, request: request}) {
		a.reply(ctx, event, threadTS, botBusyReply)
		return
	}
	announce := true
	for {
		a.runMessage(ctx, event, threadTS, request, announce)
		next, ok := a.threads.next(key)
		if !ok {
			return
		}
		event, request, announce = next.event, next.request, false
	}
}

func (a *BeamsApp) runMessage(ctx context.Context, event BeamsMessageEvent, threadTS, request string, announce bool) {
	log := logger.Get(ctx).With(
		"slack_team_id", event.TeamID,
		"slack_user_id", event.User,
		"slack_channel_id", event.Channel,
	)
	log.InfoContext(ctx, "Handling Beams Slack message")
	threadKey := event.Channel + "-" + threadTS
	// Skip the acknowledgement when the answer is instant or the user still
	// has to authorize the bot. Threads already working in a beam say so;
	// others may get a quick chat answer, and the runner reports if the
	// request moves to a beam.
	if announce && !isInstantRequest(request) && a.runner.Connected(event.TeamID, event.User) {
		if a.runner.ThreadUsesBeam(event.TeamID, event.User, threadKey) {
			a.reply(ctx, event, threadTS, beamWorkReply)
		} else {
			a.reply(ctx, event, threadTS, "Just a moment while I look into this.")
		}
	}
	ctx = withProgress(ctx, func(text string) { a.reply(ctx, event, threadTS, text) })
	if len(event.Files) > 0 {
		files, notes := a.downloadAttachments(ctx, event.Files)
		for _, note := range notes {
			a.reply(ctx, event, threadTS, note)
		}
		ctx = withAttachments(ctx, files)
	}

	result, err := a.runner.Handle(ctx, event.TeamID, event.User, threadKey, request)
	if err != nil {
		result = AskResult{Reply: fmt.Sprintf("Sorry, that failed: %s", trace.UserMessage(err))}
		log.WarnContext(ctx, "Beams Slack message failed", "error", err)
	}
	a.reply(ctx, event, threadTS, result.Reply)
	if result.DM != "" {
		// Posting to a user ID delivers to the app's direct message with them.
		if err := a.bot.PostBeamsThreadReply(ctx, event.User, "", result.DM); err != nil {
			log.ErrorContext(ctx, "Failed to send Beams direct message", "error", err)
		}
	}
	if resume := result.Resume; resume != nil {
		origin := BeamsMessageEvent{TeamID: event.TeamID, User: event.User, Channel: resume.Channel, TS: resume.ThreadTS}
		go a.handleMessage(ctx, origin, resume.Text)
	}
}

// maxAttachments and maxAttachmentSize bound what the bot downloads from one
// message.
const (
	maxAttachments    = 5
	maxAttachmentSize = 20 << 20
)

// downloadAttachments fetches a message's files. Files it cannot use are
// reported in notes rather than failing the request.
func (a *BeamsApp) downloadAttachments(ctx context.Context, files []SlackFile) ([]attachment, []string) {
	var out []attachment
	var notes []string
	for i, file := range files {
		if i == maxAttachments {
			notes = append(notes, fmt.Sprintf("I only use the first %d attachments.", maxAttachments))
			break
		}
		data, err := a.bot.DownloadSlackFile(ctx, file.URLPrivate, maxAttachmentSize)
		if err != nil {
			logger.Get(ctx).WarnContext(ctx, "Failed to download Slack attachment", "file", file.Name, "error", err)
			notes = append(notes, fmt.Sprintf("I couldn't read `%s`: %s", file.Name, trace.UserMessage(err)))
			continue
		}
		out = append(out, attachment{Name: file.Name, MimeType: file.Mimetype, Data: data})
	}
	return out, notes
}

func (a *BeamsApp) reply(ctx context.Context, event BeamsMessageEvent, threadTS, text string) {
	if err := a.bot.PostBeamsThreadReply(ctx, event.Channel, threadTS, text); err != nil {
		logger.Get(ctx).ErrorContext(ctx, "Failed to post Beams thread reply", "error", err, "slack_channel_id", event.Channel)
	}
}

type queuedMessage struct {
	event   BeamsMessageEvent
	request string
}

// threadQueue tracks which threads have a bot request running and keeps
// the newest message that arrived for each while it ran.
type threadQueue struct {
	mu      sync.Mutex
	running map[string]*queuedMessage
}

func newThreadQueue() *threadQueue {
	return &threadQueue{running: make(map[string]*queuedMessage)}
}

// start marks the thread busy and reports true if nothing was running there.
// Otherwise it queues msg, replacing any older queued message, and reports
// false.
func (q *threadQueue) start(key string, msg queuedMessage) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	if _, busy := q.running[key]; busy {
		q.running[key] = &msg
		return false
	}
	q.running[key] = nil
	return true
}

// next returns the message queued while the last run was in progress, or
// marks the thread idle when there is none.
func (q *threadQueue) next(key string) (queuedMessage, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if msg := q.running[key]; msg != nil {
		q.running[key] = nil
		return *msg, true
	}
	delete(q.running, key)
	return queuedMessage{}, false
}

// recentSet remembers a bounded number of recent keys.
type recentSet struct {
	mu    sync.Mutex
	max   int
	keys  map[string]struct{}
	order []string
}

func newRecentSet(max int) *recentSet {
	return &recentSet{max: max, keys: make(map[string]struct{})}
}

// add records key and reports whether it was new.
func (r *recentSet) add(key string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.keys[key]; ok {
		return false
	}
	r.keys[key] = struct{}{}
	r.order = append(r.order, key)
	if len(r.order) > r.max {
		delete(r.keys, r.order[0])
		r.order = r.order[1:]
	}
	return true
}
