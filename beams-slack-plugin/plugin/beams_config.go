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
	"os"
	"path/filepath"

	"github.com/gravitational/trace"

	"github.com/gravitational/teleport/integrations/access/common/auth"
)

// BeamsConfig configures the /beams Slack command.
type BeamsConfig struct {
	Enabled        bool   `toml:"enabled"`
	TSHPath        string `toml:"tsh_path"`
	Proxy          string `toml:"proxy"`
	PluginIdentity string `toml:"plugin_identity"`
	ProfilesDir    string `toml:"profiles_dir"`
	IdentityTTL    string `toml:"identity_ttl"`
	CommandTTL     string `toml:"command_timeout"`
	// ClaudeTimeout bounds /beams claude runs. ClaudeArgs replaces the default
	// extra Claude Code flags.
	ClaudeTimeout string   `toml:"claude_timeout"`
	ClaudeArgs    []string `toml:"claude_args"`
	// CodexTimeout bounds /beams codex runs. CodexArgs replaces the default
	// extra Codex flags.
	CodexTimeout string   `toml:"codex_timeout"`
	CodexArgs    []string `toml:"codex_args"`
	// ChatClaudeModel and ChatCodexModel are the models used for quick chat
	// answers through the Teleport LLM apps. DisableChat sends every request
	// to a coding agent in a beam instead.
	ChatClaudeModel string `toml:"chat_claude_model"`
	ChatCodexModel  string `toml:"chat_codex_model"`
	DisableChat     bool   `toml:"disable_chat"`
	// OmitRequestText leaves what users asked out of the request log, keeping
	// only who, when, and what kind of request.
	OmitRequestText bool `toml:"omit_request_text"`
	// ClaudeSkipApproval lets Claude Code act on bot requests without the
	// user approving a plan first. /beams claude never asks.
	ClaudeSkipApproval bool `toml:"claude_skip_approval"`
	// IncidentResponse turns on incident response for "live website is down":
	// notifying the service owner, an incident number for the thread,
	// and checking and starting nginx on the server behind the website.
	IncidentResponse bool `toml:"incident_response"`
	// IncidentSSHLogin is the login for that server; defaults to root.
	IncidentSSHLogin string `toml:"incident_ssh_login"`
	// ServiceOwner is the Slack name of the live website's owner, who
	// gets direct messages about the issue and its fix.
	ServiceOwner  string `toml:"service_owner"`
	BotName       string `toml:"bot_name"`
	DelegationTTL string `toml:"delegation_ttl"`
	// RequiredRole authorizes Slack users whose email matches a Teleport user
	// holding this role. Users lists explicit Slack ID overrides.
	RequiredRole string            `toml:"required_role"`
	Users        map[string]string `toml:"users"`
}

// checkAndSetBeamsDefaults validates [beams] and fills in its defaults. It
// runs from Config.CheckAndSetDefaults.
func (c *Config) checkAndSetBeamsDefaults() error {
	if !c.Beams.Enabled {
		return nil
	}
	if c.Beams.TSHPath == "" {
		c.Beams.TSHPath = "tsh"
	}
	if c.Beams.Proxy == "" {
		c.Beams.Proxy = c.Teleport.Addr
	}
	if c.Beams.PluginIdentity == "" {
		c.Beams.PluginIdentity = c.Teleport.Identity
	}
	if c.Beams.ProfilesDir == "" {
		c.Beams.ProfilesDir = filepath.Join(os.TempDir(), "teleport-slack-beams")
	}
	if c.Beams.IdentityTTL == "" {
		c.Beams.IdentityTTL = "15m"
	}
	if c.Beams.DelegationTTL == "" {
		c.Beams.DelegationTTL = "168h"
	}
	if c.Beams.Proxy == "" {
		return trace.BadParameter("missing required value beams.proxy")
	}
	if c.Beams.PluginIdentity == "" {
		return trace.BadParameter("missing required value beams.plugin_identity")
	}
	if c.Beams.RequiredRole == "" && len(c.Beams.Users) == 0 {
		return trace.BadParameter("beams requires required_role or users")
	}
	// Slash commands arrive through Socket Mode and therefore need an app token.
	if c.AppTokenProvider == nil {
		if c.Review.AppToken == "" {
			return trace.BadParameter("missing required value review.app_token for beams commands")
		}
		c.AppTokenProvider = auth.NewStaticAccessTokenProvider(c.Review.AppToken)
	}
	return nil
}
