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
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/stretchr/testify/require"
)

var testPeople = []SlackPerson{
	{ID: "UANNA", Name: "anna.m", RealName: "Anna Mikhailova", DisplayName: "Anna"},
	{ID: "UBOB", Name: "bob", RealName: "Bob Smith"},
	{ID: "UANNE", Name: "anne", RealName: "Anne Lee"},
}

func TestMatchPeople(t *testing.T) {
	ids := func(people []SlackPerson) []string {
		var out []string
		for _, p := range people {
			out = append(out, p.ID)
		}
		return out
	}
	require.Equal(t, []string{"UANNA"}, ids(matchPeople(testPeople, "Anna")))
	require.Equal(t, []string{"UANNA"}, ids(matchPeople(testPeople, "@anna.m")))
	require.Equal(t, []string{"UANNA"}, ids(matchPeople(testPeople, "mikhailova")))
	require.Equal(t, []string{"UBOB"}, ids(matchPeople(testPeople, "bob smith")))
	require.Equal(t, []string{"UANNA"}, ids(matchPeople(testPeople, "anna m")))
	require.Empty(t, matchPeople(testPeople, "ann"))
	require.Empty(t, matchPeople(testPeople, "carol"))
	require.Len(t, matchPeople(append(testPeople, SlackPerson{ID: "UANNA2", RealName: "Anna Smith"}), "anna"), 2)
}

func TestParseTell(t *testing.T) {
	who, message, ok := parseTell("tell Anna :: The website is down. We're on it.")
	require.True(t, ok)
	require.Equal(t, "Anna", who)
	require.Equal(t, "The website is down. We're on it.", message)
	_, _, ok = parseTell("tell Anna the website is down")
	require.False(t, ok)
	_, _, ok = parseTell("publish")
	require.False(t, ok)
}

type fakeSlack struct {
	mu    sync.Mutex
	posts []string
}

func (f *fakeSlack) find(_ context.Context, query string) ([]SlackPerson, error) {
	if id := mentionID.FindStringSubmatch(query); id != nil {
		for _, p := range testPeople {
			if p.ID == id[1] {
				return []SlackPerson{p}, nil
			}
		}
	}
	return matchPeople(testPeople, query), nil
}

func (f *fakeSlack) post(_ context.Context, channel, threadTS, text string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.posts = append(f.posts, channel+" "+threadTS+" "+text)
	return nil
}

func tellRunner(t *testing.T, reply string) (BeamsCommandRunner, *fakeSlack, string) {
	tshPath, logPath := writeFakeTSH(t)
	slack := &fakeSlack{}
	runner := BeamsCommandRunner{
		Config: BeamsConfig{
			TSHPath:     tshPath,
			ProfilesDir: t.TempDir(),
			BotName:     "teleport-beams-bot",
			Users:       map[string]string{"UALICE": "alice@example.com"},
		},
		MintIdentity: fakeMint,
		Chat: func(context.Context, beamsSession, string, []chatMessage) (string, error) {
			return reply, nil
		},
		FindPeople: slack.find,
		Post:       slack.post,
	}
	_, err := runner.Run(t.Context(), "T1", "UALICE", "connect "+testSessionID)
	require.NoError(t, err)
	return runner, slack, logPath
}

func TestBotTellsSomeoneInThread(t *testing.T) {
	runner, slack, _ := tellRunner(t, "BEAMS_BOT_ACTION: tell Anna :: The production website is down. We're on it.\nI'll let Anna know.")
	out, err := runner.Ask(t.Context(), "T1", "UALICE", "C1-1700000000.007000", "tell Anna the website is down")
	require.NoError(t, err)
	require.Equal(t, "I'll let Anna know.", out)
	require.Equal(t, []string{"C1 1700000000.007000 <@UANNA> message from <@UALICE>: The production website is down. We're on it."}, slack.posts)
}

func TestBotTellsSomeoneByDirectMessage(t *testing.T) {
	runner, slack, _ := tellRunner(t, "BEAMS_BOT_ACTION: tell <@UANNA> :: The website is down. We're on it.\nOn it.")
	out, err := runner.Ask(t.Context(), "T1", "UALICE", "D1-1700000000.008000", "tell <@UANNA> the website is down")
	require.NoError(t, err)
	require.Equal(t, "On it.\n\nSent Anna Mikhailova a direct message.", out)
	require.Equal(t, []string{"UANNA  Message from <@UALICE>: The website is down. We're on it."}, slack.posts)
}

func TestBotTellNeedsOnePerson(t *testing.T) {
	runner, slack, _ := tellRunner(t, "BEAMS_BOT_ACTION: tell An :: hi\nBEAMS_BOT_ACTION: tell carol :: hi\nSure.")
	testPeople = append(testPeople, SlackPerson{ID: "UAN2", RealName: "An Two"}, SlackPerson{ID: "UAN3", RealName: "An Three"})
	t.Cleanup(func() { testPeople = testPeople[:3] })
	out, err := runner.Ask(t.Context(), "T1", "UALICE", "C1-1700000000.009000", "tell An and carol hi")
	require.NoError(t, err)
	require.Contains(t, out, "More than one person matches An (An Two, An Three)")
	require.Contains(t, out, "I couldn't find anyone in Slack called carol")
	require.Empty(t, slack.posts)
}

func TestBotTellsBeforeBeamWork(t *testing.T) {
	runner, slack, logPath := tellRunner(t, "BEAMS_BOT_ACTION: tell Anna :: The production website is down. We're on it.\nHANDOFF")
	_, err := runner.Ask(t.Context(), "T1", "UALICE", "C1-1700000000.010000", "tell Anna the website is down and write a script to test it")
	require.NoError(t, err)
	require.Len(t, slack.posts, 1)

	// The coding agent knows the message already went out.
	calls, err := os.ReadFile(logPath)
	require.NoError(t, err)
	require.Contains(t, string(calls), "already sent these messages")
	require.Contains(t, string(calls), "Anna Mikhailova: The production website is down.")

	// A repeat from the agent is not sent twice.
	s, err := runner.newSession(t.Context(), "T1", "UALICE")
	require.NoError(t, err)
	told := map[string]string{}
	require.Empty(t, s.tell(t.Context(), "C1", "1.2", "tell Anna :: once", told))
	require.Empty(t, s.tell(t.Context(), "C1", "1.2", "tell anna.m :: twice", told))
	require.Len(t, slack.posts, 2)
	require.True(t, strings.HasSuffix(slack.posts[1], "once"))
}

func TestBotHandsOffAfterASentence(t *testing.T) {
	runner, slack, logPath := tellRunner(t, "BEAMS_BOT_ACTION: tell Anna :: The website is down. We're on it.\nGot it, I've let Anna know. Now let's write that script.\n\nHANDOFF")
	out, err := runner.Ask(t.Context(), "T1", "UALICE", "C1-1700000000.011000", "tell Anna the website is down and set up a monitor")
	require.NoError(t, err)
	require.NotContains(t, out, "HANDOFF")
	require.Len(t, slack.posts, 1)
	calls, err := os.ReadFile(logPath)
	require.NoError(t, err)
	require.Contains(t, string(calls), "beams exec mine 'claude'")
}
