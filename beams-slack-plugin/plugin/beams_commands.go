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
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode"

	"github.com/google/uuid"
	"github.com/gravitational/trace"
	"github.com/mattn/go-shellwords"
	"golang.org/x/crypto/ssh"
	"google.golang.org/protobuf/types/known/durationpb"

	delegationv1 "github.com/gravitational/teleport/api/gen/proto/go/teleport/delegation/v1"
	"github.com/gravitational/teleport/api/identityfile"
	"github.com/gravitational/teleport/api/utils/keys"
	"github.com/gravitational/teleport/lib/services"
)

const (
	defaultBeamsCommandTimeout = 2 * time.Minute
	defaultAgentTimeout        = 15 * time.Minute
)

// defaultClaudeArgs and defaultCodexArgs let the agents run tools without
// interactive approval, which non-interactive runs cannot ask for. Beams are
// ephemeral runtimes.
var (
	defaultClaudeArgs = []string{"--dangerously-skip-permissions"}
	defaultCodexArgs  = []string{"--dangerously-bypass-approvals-and-sandbox"}
)

var (
	safeSlackID = regexp.MustCompile(`^[A-Z0-9]+$`)
	ansiEscape  = regexp.MustCompile(`\x1b\[[0-9;]*[A-Za-z]`)
)

// BeamsCommandRunner executes non-interactive tsh beams commands for Slack
// users. Every beam action runs as the user's own Teleport identity, minted
// through a delegation session the user granted to the plugin's bot, so the
// user owns their beams and Teleport keeps users apart.
type BeamsCommandRunner struct {
	Config           BeamsConfig
	DelegationClient delegationv1.DelegationSessionServiceClient
	// LookupEmail returns the email address of a Slack user.
	LookupEmail func(ctx context.Context, userID string) (string, error)
	// Users reads Teleport users to authorize Slack users by role.
	Users services.UserOrLoginStateGetter
	// MintIdentity writes a delegated identity for a session to path. It
	// defaults to minting through DelegationClient; tests replace it.
	MintIdentity func(ctx context.Context, sessionID, username, path string) error
	// Apps lists Teleport apps, used to find the LLM apps that chat answers
	// go through. Chat is off when both Apps and Chat are nil.
	Apps appLister
	// ClusterName is the Teleport cluster the LLM apps belong to.
	ClusterName string
	// Chat answers a chat turn as the user. It defaults to calling the
	// Teleport LLM app for the agent; tests replace it.
	Chat func(ctx context.Context, s beamsSession, agent string, messages []chatMessage) (string, error)
	// FindPeople looks up Slack users by name or <@ID> mention, for messages
	// the bot sends for a user.
	FindPeople func(ctx context.Context, query string) ([]SlackPerson, error)
	// Post sends a Slack message, in a thread when threadTS is set.
	Post func(ctx context.Context, channel, threadTS, text string) error
	// Background runs work a request leaves going after it returns, such as
	// recording a chat answer in a beam. It defaults to a new goroutine;
	// tests replace it so they can wait for that work.
	Background func(f func())
}

// beamsSession is the per-command execution context for one Slack user.
type beamsSession struct {
	runner      BeamsCommandRunner
	teamID      string
	slackUserID string
	profileDir  string
	username    string
	delegation  string
	sessionPath string
	// timeout overrides beams.command_timeout when set.
	timeout time.Duration
}

func (r BeamsCommandRunner) Run(ctx context.Context, teamID, userID, text string) (string, error) {
	if !safeSlackID.MatchString(teamID) || !safeSlackID.MatchString(userID) {
		return "", trace.BadParameter("invalid Slack identity")
	}
	words, err := parseCommand(text)
	if err != nil {
		return "", trace.Wrap(err)
	}
	if len(words) == 0 || words[0] == "help" {
		return beamsHelp(), nil
	}

	s, err := r.newSession(ctx, teamID, userID)
	if err != nil {
		return "", trace.Wrap(err)
	}
	username := s.username

	if words[0] != "connect" {
		// Leave session IDs out of the log.
		s.logRequest(ctx, "slash", text, "command", words[0])
	}

	var out string
	switch words[0] {
	case "connect":
		return s.connect(ctx, words[1:])
	case "disconnect":
		if err := os.Remove(s.sessionPath); err != nil && !os.IsNotExist(err) {
			return "", trace.Wrap(err)
		}
		return "Disconnected. Run `/beams connect` to use Beams again.", nil
	case "status":
		if s.delegation != "" {
			return fmt.Sprintf("Connected to %s as %s.", r.Config.Proxy, username), nil
		}
		return "Not connected. Run `/beams connect`.", nil
	case "ls":
		out, err = s.list(ctx)
	case "add":
		out, err = s.add(ctx, words[1:])
		out = codeBlock(out)
	case "exec", "publish", "unpublish", "rm", "scp":
		out, err = s.target(ctx, words)
		out = codeBlock(out)
	case "claude":
		out, err = s.claude(ctx, words[1:])
		out = codeBlock(out)
	case "codex":
		out, err = s.codex(ctx, words[1:])
		out = codeBlock(out)
	case "ssh":
		return "", trace.BadParameter("interactive SSH is unavailable in Slack; use /beams exec <name> <command>")
	default:
		return "", trace.BadParameter("unknown command %q\n%s", words[0], beamsHelp())
	}
	if needsAuthorization(err) {
		return s.authorizationReply("Run `/beams connect <session-id>` with the ID it prints."), nil
	}
	return out, err
}

// needsAuthorizationError means the user has no usable delegation session:
// they never connected, or the session expired or was revoked.
type needsAuthorizationError struct{ err error }

func (e *needsAuthorizationError) Error() string { return e.err.Error() }
func (e *needsAuthorizationError) Unwrap() error { return e.err }

func needsAuthorization(err error) bool {
	var target *needsAuthorizationError
	return errors.As(err, &target)
}

// authorizationReply forgets an unusable session and asks the user to grant a
// new one. next tells them how to hand the session ID back.
func (s beamsSession) authorizationReply(next string) string {
	prefix := ""
	if s.delegation != "" {
		_ = os.Remove(s.sessionPath)
		prefix = "Your authorization has expired.\n"
	}
	return s.runner.authorizationMessage(prefix, next, s.username)
}

// newSession authorizes a Slack user and loads their delegation state.
func (r BeamsCommandRunner) newSession(ctx context.Context, teamID, userID string) (beamsSession, error) {
	if !safeSlackID.MatchString(teamID) || !safeSlackID.MatchString(userID) {
		return beamsSession{}, trace.BadParameter("invalid Slack identity")
	}
	profileDir := filepath.Join(r.Config.ProfilesDir, teamID, userID)
	if err := os.MkdirAll(profileDir, 0o700); err != nil {
		return beamsSession{}, trace.Wrap(err)
	}
	username, err := r.resolveUser(ctx, userID)
	if err != nil {
		return beamsSession{}, trace.Wrap(err)
	}
	s := beamsSession{
		runner:      r,
		teamID:      teamID,
		slackUserID: userID,
		profileDir:  profileDir,
		username:    username,
		sessionPath: filepath.Join(profileDir, "delegation-session"),
	}
	if sessionID, err := os.ReadFile(s.sessionPath); err == nil {
		s.delegation = strings.TrimSpace(string(sessionID))
	} else if !os.IsNotExist(err) {
		return beamsSession{}, trace.Wrap(err)
	}

	return s, nil
}

func (s beamsSession) connect(ctx context.Context, args []string) (string, error) {
	if s.runner.Config.BotName == "" {
		return "", trace.BadParameter("delegation is not configured; set beams.bot_name")
	}
	if len(args) == 0 {
		return s.runner.authorizationMessage("", "Run `/beams connect <session-id>` with the ID it prints.", s.username), nil
	}
	if len(args) != 1 {
		return "", trace.BadParameter("usage: /beams connect [<delegation-session-id>]")
	}
	sessionID, err := uuid.Parse(args[0])
	if err != nil {
		return "", trace.BadParameter("invalid delegation session ID")
	}
	s.delegation = sessionID.String()
	// Minting a certificate proves the session is valid, belongs to the
	// mapped user, and was granted to this bot.
	_, cleanup, err := s.userIdentity(ctx)
	if err != nil {
		return "", trace.Wrap(err, "validating delegation session")
	}
	cleanup()
	if err := os.WriteFile(s.sessionPath, []byte(s.delegation), 0o600); err != nil {
		return "", trace.Wrap(err)
	}
	return fmt.Sprintf("Connected to %s as %s. Your beams are owned by your Teleport user.", s.runner.Config.Proxy, s.username), nil
}

// delegatedBeams returns the beams owned by the user's Teleport identity.
func (s beamsSession) delegatedBeams(ctx context.Context) ([]map[string]any, error) {
	out, err := s.userTSH(ctx, "beams", "ls", "--format=json")
	if err != nil {
		return nil, trace.Wrap(err)
	}
	return decodeBeams(out)
}

// userTSH runs tsh with a freshly minted identity for the user.
func (s beamsSession) userTSH(ctx context.Context, args ...string) (string, error) {
	identityPath, cleanup, err := s.userIdentity(ctx)
	if err != nil {
		return "", trace.Wrap(err)
	}
	defer cleanup()
	return s.tsh(ctx, identityPath, args...)
}

func (s beamsSession) list(ctx context.Context) (string, error) {
	beams, err := s.delegatedBeams(ctx)
	if err != nil {
		return "", trace.Wrap(err)
	}
	return truncateSlackOutput(formatBeamsList(beams, time.Now(), "You don't have any beams yet. Create one with `/beams add`.")), nil
}

func (s beamsSession) add(ctx context.Context, args []string) (string, error) {
	out, _, err := s.createBeam(ctx, args)
	if err != nil {
		return "", trace.Wrap(err)
	}
	return truncateSlackOutput(out), nil
}

// createBeam creates a beam owned by the user. It returns the tsh output and
// the new beam's name.
func (s beamsSession) createBeam(ctx context.Context, args []string) (string, string, error) {
	tshArgs := append([]string{"beams", "add", "--no-console", "--format=json"}, args...)
	out, err := s.userTSH(ctx, tshArgs...)
	if err != nil {
		return "", "", trace.Wrap(err)
	}
	var beam struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal([]byte(out), &beam); err != nil || beam.ID == "" {
		return "", "", trace.BadParameter("beam was created but its name could not be read from tsh output")
	}
	return out, beam.ID, nil
}

// target runs a command that acts on named beams as the user; Teleport only
// lets them reach beams they own.
func (s beamsSession) target(ctx context.Context, words []string) (string, error) {
	if len(beamTargets(words)) == 0 {
		return "", trace.BadParameter("usage: /beams %s <name> ...", words[0])
	}
	out, err := s.userTSH(ctx, append([]string{"beams"}, words...)...)
	if err != nil {
		return "", trace.Wrap(err)
	}
	return truncateSlackOutput(out), nil
}

// codingAgent describes how to run a coding agent CLI non-interactively in a
// beam for /beams claude and /beams codex.
type codingAgent struct {
	// name is the slash subcommand and the config key prefix.
	name string
	// timeout is the configured run limit; empty means defaultAgentTimeout.
	timeout string
	// command builds the agent command line for a prompt, optionally
	// continuing the agent's most recent conversation in the beam.
	command func(continued bool, prompt string) []string
}

// runAgent parses "<name> [--continue] <prompt...>" and runs the agent in
// that beam, returning its final answer.
func (s beamsSession) runAgent(ctx context.Context, agent codingAgent, args []string) (string, error) {
	usage := fmt.Sprintf("usage: /beams %s <name> [--continue] <prompt...>", agent.name)
	if len(args) < 2 {
		return "", trace.BadParameter("%s", usage)
	}
	name, args := args[0], args[1:]
	continued := false
	if args[0] == "--continue" || args[0] == "-c" {
		continued = true
		args = args[1:]
	}
	if len(args) == 0 {
		return "", trace.BadParameter("%s", usage)
	}

	s.timeout = defaultAgentTimeout
	if agent.timeout != "" {
		timeout, err := time.ParseDuration(agent.timeout)
		if err != nil {
			return "", trace.BadParameter("invalid beams.%s_timeout: %v", agent.name, err)
		}
		s.timeout = timeout
	}
	// The remote side runs the command through a shell, so pass one quoted
	// command string rather than separate arguments.
	command := agent.command(continued, strings.Join(args, " "))
	// Without a closed stdin, Claude Code waits 3 seconds for piped input.
	return s.target(ctx, []string{"exec", name, shellQuoteJoin(command) + " < /dev/null"})
}

// claude runs Claude Code in print mode inside a beam and returns its answer.
func (s beamsSession) claude(ctx context.Context, args []string) (string, error) {
	extraArgs := s.runner.Config.ClaudeArgs
	if extraArgs == nil {
		extraArgs = defaultClaudeArgs
	}
	return s.runAgent(ctx, codingAgent{
		name:    "claude",
		timeout: s.runner.Config.ClaudeTimeout,
		command: func(continued bool, prompt string) []string {
			command := append([]string{"claude", "-p"}, extraArgs...)
			if continued {
				command = append(command, "--continue")
			}
			return append(command, prompt)
		},
	}, args)
}

// claudeForBot runs Claude Code for a bot request. While planning it runs in
// plan mode, where it can look around but not change anything; once the user
// approves, it may use every tool.
func (s beamsSession) claudeForBot(ctx context.Context, args []string, planning bool) (string, error) {
	extraArgs := s.runner.Config.ClaudeArgs
	if extraArgs == nil {
		extraArgs = defaultClaudeArgs
	}
	extraArgs = withoutPermissionFlags(extraArgs)
	mode := []string{"--dangerously-skip-permissions"}
	if planning {
		mode = []string{"--permission-mode", "plan"}
	}
	return s.runAgent(ctx, codingAgent{
		name:    "claude",
		timeout: s.runner.Config.ClaudeTimeout,
		command: func(continued bool, prompt string) []string {
			command := append(append([]string{"claude", "-p"}, extraArgs...), mode...)
			if continued {
				command = append(command, "--continue")
			}
			return append(command, prompt)
		},
	}, args)
}

// withoutPermissionFlags drops Claude Code permission flags, which the bot
// sets itself.
func withoutPermissionFlags(args []string) []string {
	var out []string
	for i := 0; i < len(args); i++ {
		switch {
		case args[i] == "--dangerously-skip-permissions", args[i] == "--allow-dangerously-skip-permissions",
			strings.HasPrefix(args[i], "--permission-mode="):
		case args[i] == "--permission-mode":
			i++
		default:
			out = append(out, args[i])
		}
	}
	return out
}

// codex runs Codex non-interactively inside a beam and returns its answer.
// Codex prints progress to stderr and only the final message to stdout.
func (s beamsSession) codex(ctx context.Context, args []string) (string, error) {
	extraArgs := s.runner.Config.CodexArgs
	if extraArgs == nil {
		extraArgs = defaultCodexArgs
	}
	return s.runAgent(ctx, codingAgent{
		name:    "codex",
		timeout: s.runner.Config.CodexTimeout,
		command: func(continued bool, prompt string) []string {
			// A beam's home directory is not a Git repository.
			command := append([]string{"codex", "exec", "--skip-git-repo-check"}, extraArgs...)
			if continued {
				command = append(command, "resume", "--last")
			}
			return append(command, prompt)
		},
	}, args)
}

// shellQuoteJoin quotes each argument for a POSIX shell.
func shellQuoteJoin(args []string) string {
	quoted := make([]string, len(args))
	for i, arg := range args {
		quoted[i] = "'" + strings.ReplaceAll(arg, "'", `'\''`) + "'"
	}
	return strings.Join(quoted, " ")
}

// userIdentity mints a short-lived identity for the user's delegation session
// into a private temporary file.
func (s beamsSession) userIdentity(ctx context.Context) (string, func(), error) {
	if s.delegation == "" {
		return "", nil, &needsAuthorizationError{trace.AccessDenied("not connected")}
	}
	f, err := os.CreateTemp(s.profileDir, "identity-*")
	if err != nil {
		return "", nil, trace.Wrap(err)
	}
	path := f.Name()
	_ = f.Close()
	cleanup := func() { _ = os.Remove(path) }
	mint := s.runner.MintIdentity
	if mint == nil {
		mint = s.runner.generateDelegatedIdentity
	}
	if err := mint(ctx, s.delegation, s.username, path); err != nil {
		cleanup()
		if trace.IsAccessDenied(err) || trace.IsNotFound(err) {
			return "", nil, &needsAuthorizationError{err}
		}
		return "", nil, trace.Wrap(err, "minting your Teleport identity")
	}
	return path, cleanup, nil
}

// A new beam rejects SSH until Teleport assigns it a node, and tsh fails
// immediately in that case instead of waiting, so commands retry it.
const beamNotReady = "is not ready to accept SSH connections"

var (
	beamReadyTimeout = 3 * time.Minute
	beamReadyPoll    = 5 * time.Second
)

func (s beamsSession) tsh(ctx context.Context, identityPath string, args ...string) (string, error) {
	deadline := time.Now().Add(beamReadyTimeout)
	for {
		out, stderr, err := s.tshOnce(ctx, identityPath, args...)
		if err == nil || !strings.Contains(stderr, beamNotReady) || time.Now().After(deadline) {
			return out, err
		}
		select {
		case <-ctx.Done():
			return "", trace.Wrap(ctx.Err())
		case <-time.After(beamReadyPoll):
		}
	}
}

func (s beamsSession) tshOnce(ctx context.Context, identityPath string, args ...string) (stdout, stderr string, err error) {
	timeout := defaultBeamsCommandTimeout
	if s.timeout > 0 {
		timeout = s.timeout
	} else if s.runner.Config.CommandTTL != "" {
		parsed, parseErr := time.ParseDuration(s.runner.Config.CommandTTL)
		if parseErr != nil {
			return "", "", trace.BadParameter("invalid beams.command_timeout: %v", parseErr)
		}
		timeout = parsed
	}
	runCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	tshArgs := append([]string{
		"--proxy=" + s.runner.Config.Proxy,
		"--identity=" + identityPath,
	}, args...)
	cmd := exec.CommandContext(runCtx, s.runner.Config.TSHPath, tshArgs...)
	cmd.Env = append(os.Environ(), "TELEPORT_HOME="+s.profileDir, "NO_COLOR=1")
	var outBuf, errBuf bytes.Buffer
	cmd.Stdout, cmd.Stderr = &outBuf, &errBuf
	if err := cmd.Run(); err != nil {
		stderr = cleanTSHError(errBuf.String())
		if runCtx.Err() != nil {
			return "", stderr, trace.LimitExceeded("command timed out")
		}
		// A command that fails in a beam, such as a coding agent hitting a
		// model error, explains why on stdout, so keep the end of it.
		detail := stderr
		if tail := outputTail(outBuf.String()); tail != "" {
			detail = tail + "\n" + stderr
		}
		return "", stderr, trace.Wrap(err, detail)
	}
	return stripANSI(outBuf.String()), "", nil
}

func decodeBeams(output string) ([]map[string]any, error) {
	var beams []map[string]any
	if strings.TrimSpace(output) == "" {
		return nil, nil
	}
	if err := json.Unmarshal([]byte(output), &beams); err != nil {
		return nil, trace.Wrap(err, "decoding tsh beams ls output")
	}
	return beams, nil
}

// beamTargets returns the beam names a command acts on: the first positional
// argument, or for scp the host part of every <beam>:<path> argument.
func beamTargets(words []string) []string {
	var names []string
	for _, word := range words[1:] {
		if strings.HasPrefix(word, "-") {
			continue
		}
		if words[0] != "scp" {
			return []string{word}
		}
		if name, _, ok := strings.Cut(word, ":"); ok && name != "" {
			names = append(names, name)
		}
	}
	return names
}

// parseCommand splits a slash command into words like a shell would. Agent
// prompts are free text, so for claude and codex only the beam name and
// --continue are split off and the rest is kept exactly as typed; apostrophes
// such as "what's" then need no quoting.
func parseCommand(text string) ([]string, error) {
	if fields := strings.Fields(text); len(fields) > 0 && (fields[0] == "claude" || fields[0] == "codex") {
		return agentWords(text), nil
	}
	words, err := shellwords.Parse(text)
	if err != nil {
		return nil, trace.BadParameter("couldn't read the command (%v); wrap arguments that contain quotes or apostrophes in double quotes", err)
	}
	for i, word := range words {
		words[i] = trimSlackCode(word)
	}
	return words, nil
}

// trimSlackCode removes the backticks Slack keeps when a user pastes text
// that was shown as code, such as a beam name copied from a reply.
func trimSlackCode(word string) string {
	return strings.Trim(word, "`")
}

// agentWords splits "<agent> <name> [--continue|-c] <prompt...>", keeping the
// prompt as one word. A prompt wrapped entirely in quotes is unwrapped.
func agentWords(text string) []string {
	rest := strings.TrimSpace(text)
	var words []string
	for rest != "" && len(words) < 3 {
		word, after := rest, ""
		if i := strings.IndexFunc(rest, unicode.IsSpace); i >= 0 {
			word, after = rest[:i], rest[i:]
		}
		if len(words) == 2 && word != "--continue" && word != "-c" {
			break
		}
		words = append(words, trimSlackCode(word))
		rest = strings.TrimSpace(after)
	}
	if len(rest) >= 2 && (rest[0] == '"' || rest[0] == '\'') && rest[len(rest)-1] == rest[0] {
		rest = rest[1 : len(rest)-1]
	}
	if rest != "" {
		words = append(words, rest)
	}
	return words
}

// codeBlock shows multi-line tsh output as a Slack code block.
func codeBlock(output string) string {
	output = strings.TrimSpace(output)
	if !strings.Contains(output, "\n") {
		return output
	}
	return "```" + output + "```"
}

// cleanTSHError drops tsh's "cannot relogin" line, which accompanies every
// failure when tsh cannot prompt and hides the real error.
func cleanTSHError(stderr string) string {
	var lines []string
	for _, line := range strings.Split(stripANSI(stderr), "\n") {
		if strings.Contains(line, "cannot relogin in non-interactive session") {
			continue
		}
		lines = append(lines, line)
	}
	return strings.TrimSpace(strings.Join(lines, "\n"))
}

// maxFailureOutput bounds how much of a failed command's output is reported.
const maxFailureOutput = 1000

// outputTail returns the end of a failed command's output.
func outputTail(stdout string) string {
	tail := strings.TrimSpace(stripANSI(stdout))
	if len(tail) > maxFailureOutput {
		tail = "…" + strings.ToValidUTF8(tail[len(tail)-maxFailureOutput:], "")
	}
	return tail
}

func stripANSI(s string) string {
	return ansiEscape.ReplaceAllString(s, "")
}

// resolveUser maps a Slack user to the Teleport username it acts for. Explicit
// beams.users entries win; otherwise the Slack email must name a Teleport user
// holding beams.required_role.
func (r BeamsCommandRunner) resolveUser(ctx context.Context, slackUserID string) (string, error) {
	if username, ok := r.Config.Users[slackUserID]; ok && username != "" {
		return username, nil
	}
	if r.Config.RequiredRole == "" || r.LookupEmail == nil || r.Users == nil {
		return "", trace.AccessDenied("your Slack account is not allowed to use Beams")
	}

	email, err := r.LookupEmail(ctx, slackUserID)
	if err != nil {
		return "", trace.Wrap(err, "looking up Slack email (the Slack app needs users:read.email)")
	}
	if email == "" {
		return "", trace.AccessDenied("your Slack profile has no email address; the Slack app needs users:read.email")
	}

	user, err := services.GetUserOrLoginState(ctx, r.Users, email)
	if trace.IsNotFound(err) {
		return "", trace.AccessDenied("no Teleport user %q; sign in to Teleport once, then retry", email)
	}
	if err != nil {
		return "", trace.Wrap(err, "looking up Teleport user %q", email)
	}
	if user.IsBot() || !slices.Contains(user.GetRoles(), r.Config.RequiredRole) {
		return "", trace.AccessDenied("Teleport user %q does not have the %q role", email, r.Config.RequiredRole)
	}
	return email, nil
}

// authorizationMessage asks the user to let the bot create beams as them. next
// says how to give the session ID back.
func (r BeamsCommandRunner) authorizationMessage(prefix, next, username string) string {
	return fmt.Sprintf(
		"%s*Before we can get started, you need to allow me to create beams as you.*\n\n1. Run this in a terminal (`tsh` signs you in to Teleport if needed):\n```%s```\n2. %s",
		prefix,
		r.delegationCommand(username),
		next,
	)
}

// delegationCommand is the tsh command a user runs to let the bot act as them.
// --user makes tsh sign in as the user's Teleport account, so the session is
// created for the user the plugin matched from their Slack email.
func (r BeamsCommandRunner) delegationCommand(username string) string {
	return fmt.Sprintf(
		"tsh delegation create-session --proxy=%s --user=%s --bot=%s --allow-all --session-ttl=%s",
		r.Config.Proxy,
		username,
		r.Config.BotName,
		r.Config.DelegationTTL,
	)
}

// mintDelegatedCerts asks the delegation API for certificates for the user
// behind sessionID, optionally routed to an app, and checks they were issued
// for username.
func (r BeamsCommandRunner) mintDelegatedCerts(ctx context.Context, sessionID, username string, app *delegationv1.RouteToApp) (*rsa.PrivateKey, *delegationv1.GenerateCertsResponse, error) {
	if r.DelegationClient == nil {
		return nil, nil, trace.BadParameter("delegation client is not configured")
	}
	ttl, err := time.ParseDuration(r.Config.IdentityTTL)
	if err != nil {
		return nil, nil, trace.BadParameter("invalid beams.identity_ttl: %v", err)
	}
	if ttl <= 0 || ttl > time.Hour {
		return nil, nil, trace.BadParameter("beams.identity_ttl must be between 1s and 1h")
	}

	privateKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		return nil, nil, trace.Wrap(err)
	}
	sshPublicKey, err := ssh.NewPublicKey(privateKey.Public())
	if err != nil {
		return nil, nil, trace.Wrap(err)
	}
	tlsPublicKey, err := keys.MarshalPublicKey(privateKey.Public())
	if err != nil {
		return nil, nil, trace.Wrap(err)
	}
	req := &delegationv1.GenerateCertsRequest{
		DelegationSessionId: sessionID,
		SshPublicKey:        ssh.MarshalAuthorizedKey(sshPublicKey),
		TlsPublicKey:        tlsPublicKey,
		Ttl:                 durationpb.New(ttl),
	}
	if app != nil {
		req.Routing = &delegationv1.GenerateCertsRequest_RouteToApp{RouteToApp: app}
	}
	response, err := r.DelegationClient.GenerateCerts(ctx, req)
	if err != nil {
		return nil, nil, trace.Wrap(err)
	}
	certificate, _, err := keys.X509Certificate(response.GetTls())
	if err != nil {
		return nil, nil, trace.Wrap(err)
	}
	if certificate.Subject.CommonName != username {
		return nil, nil, trace.AccessDenied("delegation session belongs to %q, expected %q", certificate.Subject.CommonName, username)
	}
	return privateKey, response, nil
}

func (r BeamsCommandRunner) generateDelegatedIdentity(ctx context.Context, sessionID, username, identityPath string) error {
	privateKey, response, err := r.mintDelegatedCerts(ctx, sessionID, username, nil)
	if err != nil {
		return trace.Wrap(err)
	}
	privateKeyPEM, err := keys.MarshalPrivateKey(privateKey)
	if err != nil {
		return trace.Wrap(err)
	}
	botIdentity, err := identityfile.ReadFile(r.Config.PluginIdentity)
	if err != nil {
		return trace.Wrap(err, "reading Machine ID identity")
	}
	delegatedIdentity := &identityfile.IdentityFile{
		PrivateKey: privateKeyPEM,
		Certs: identityfile.Certs{
			SSH: response.GetSsh(),
			TLS: response.GetTls(),
		},
		CACerts: botIdentity.CACerts,
	}
	if err := identityfile.Write(delegatedIdentity, identityPath); err != nil {
		return trace.Wrap(err)
	}
	return nil
}

func beamsHelp() string {
	return strings.TrimSpace(`/beams connect [<delegation-session-id>]
/beams disconnect
/beams status
/beams ls
/beams add [--region=<region>]
/beams exec <name> <command...>
/beams claude <name> [--continue] <prompt...>
/beams codex <name> [--continue] <prompt...>
/beams publish <name> [--tcp]
/beams unpublish <name>
/beams rm <name>
/beams scp <src> <dst>`)
}

func truncateSlackOutput(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return "Done."
	}
	const max = 2800
	if len(s) > max {
		return fmt.Sprintf("%s\n… output truncated", s[:max])
	}
	return s
}
