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
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/gravitational/trace"
)

const (
	// maxChatImages and maxChatImageSize keep a chat request with images under
	// Teleport's 10 MiB limit for proxied model requests once base64-encoded.
	maxChatImages    = 2
	maxChatImageSize = 3500 << 10
	// beamUploadDir is where attachments are copied in a beam.
	beamUploadDir = "/tmp/slack-attachments"
)

// chatImageTypes are the image formats both model APIs accept.
var chatImageTypes = map[string]bool{"image/jpeg": true, "image/png": true, "image/gif": true, "image/webp": true}

var unsafeFileChars = regexp.MustCompile(`[^A-Za-z0-9._-]+`)

// attachment is a file from a Slack message.
type attachment struct {
	Name     string
	MimeType string
	Data     []byte
}

type attachmentsKey struct{}

// withAttachments attaches a message's downloaded files to the request.
func withAttachments(ctx context.Context, files []attachment) context.Context {
	return context.WithValue(ctx, attachmentsKey{}, files)
}

func attachmentsFrom(ctx context.Context) []attachment {
	files, _ := ctx.Value(attachmentsKey{}).([]attachment)
	return files
}

// chatImages returns the attachments a chat answer can include, and whether
// every attachment fits. Anything else (other file types, large or many
// images) needs a coding agent in a beam.
func chatImages(files []attachment) (images []attachment, allFit bool) {
	for _, f := range files {
		if !chatImageTypes[f.MimeType] || len(f.Data) > maxChatImageSize || len(images) == maxChatImages {
			return nil, false
		}
		images = append(images, f)
	}
	return images, true
}

// base64 encodes an attachment for the model APIs.
func (a attachment) base64() string {
	return base64.StdEncoding.EncodeToString(a.Data)
}

// describeAttachments notes attachments in text kept for chat history.
func describeAttachments(text string, files []attachment) string {
	for _, f := range files {
		text += fmt.Sprintf("\n[attached %s]", f.Name)
	}
	return text
}

// uploadAttachments copies the request's attachments into the beam as the
// user and returns their paths there.
func (s beamsSession) uploadAttachments(ctx context.Context, beam string, files []attachment) ([]string, error) {
	var paths []string
	for _, f := range files {
		name := unsafeFileChars.ReplaceAllString(filepath.Base(f.Name), "_")
		if name == "" || name == "." || name == ".." {
			name = "attachment"
		}
		local, err := os.CreateTemp(s.profileDir, "upload-*")
		if err != nil {
			return nil, trace.Wrap(err)
		}
		_, writeErr := local.Write(f.Data)
		closeErr := local.Close()
		if err := trace.NewAggregate(writeErr, closeErr); err != nil {
			os.Remove(local.Name())
			return nil, trace.Wrap(err)
		}
		remote := beamUploadDir + "/" + name
		// tsh beams scp does not create directories, so make one first.
		_, err = s.userTSH(ctx, "beams", "exec", beam, "mkdir -p "+beamUploadDir)
		if err == nil {
			_, err = s.userTSH(ctx, "beams", "scp", local.Name(), beam+":"+remote)
		}
		os.Remove(local.Name())
		if err != nil {
			return nil, trace.Wrap(err, "copying %s into beam %q", f.Name, beam)
		}
		paths = append(paths, remote)
	}
	return paths, nil
}

// attachmentPrompt tells the coding agent where the user's files are.
func attachmentPrompt(paths []string) string {
	if len(paths) == 0 {
		return ""
	}
	return "The user attached these files, now in this runtime:\n- " + strings.Join(paths, "\n- ") + "\n"
}
