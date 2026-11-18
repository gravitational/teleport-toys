# Everything runs against a Teleport checkout in .teleport/ that
# scripts/teleport-tree.sh prepares (Teleport + upstream.patch + plugin/).
# Go fetches the toolchain Teleport's go.mod asks for if yours is older.

TREE := $(or $(TELEPORT_TREE),$(CURDIR)/.teleport)
PKG  := ./integrations/access/slack

.PHONY: tree test build vet fmt patch image up clean

tree:
	@TELEPORT_TREE=$(TREE) scripts/teleport-tree.sh

test: tree
	cd $(TREE) && go test -count=1 $(PKG)/

vet: tree
	cd $(TREE) && go vet $(PKG)/

build: tree
	cd $(TREE) && go build -o $(CURDIR)/bin/teleport-slack $(PKG)/cmd/teleport-slack

fmt:
	gofmt -w plugin/*.go

# After editing Teleport's own files under $(TREE)/integrations/access/slack.
patch:
	@TELEPORT_TREE=$(TREE) scripts/teleport-tree.sh patch

image:
	docker build -t beams-slack-plugin:local .

up:
	docker compose up -d --build

clean:
	rm -rf .teleport bin
