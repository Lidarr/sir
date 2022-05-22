.PHONY: docs createextension createsql dropsql installsql installamqp onlinedocs test triggers index

docs:
	cd docs && make html

test:
	python -m unittest discover

triggers: createextension createsql createdropsql

createextension:
	python -m sir extension

createsql:
	python -m sir triggers --entity-type artist --entity-type release-group

createdropsql:
	$(MB_SERVER_PATH)/admin/GenerateSQLScripts.pl sql/

installsql:
	$(MB_SERVER_PATH)/admin/psql -f sql/CreateExtension.sql
	$(MB_SERVER_PATH)/admin/psql -f sql/CreateFunctions.sql
	$(MB_SERVER_PATH)/admin/psql -f sql/CreateTriggers.sql

dropsql:
	$(MB_SERVER_PATH)/admin/psql -f sql/DropTriggers.sql
	$(MB_SERVER_PATH)/admin/psql -f sql/DropFunctions.sql

installamqp:
	python -m sir amqp_setup

install: installamqp triggers installsql

index:
	python -m sir reindex --entity-type artist --entity-type release-group
