/*
 * MetadataManager.actor.cpp
 *
 * This source file is part of the FoundationDB open source project
 *
 * Copyright 2013-2018 Apple Inc. and the FoundationDB project authors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "DocumentError.h"
#include "ExtStructs.h"
#include "ExtUtil.actor.h"
#include "MetadataManager.h"

using namespace FDB;

Future<uint64_t> getMetadataVersion(Reference<Transaction> tr, Reference<DirectorySubspace> metadataDirectory) {
	std::string versionKey = metadataDirectory->key().toString() +
	                         DataValue(DocLayerConstants::VERSION_KEY, DVTypeCode::STRING).encode_key_part();
	Future<Optional<FDBStandalone<StringRef>>> fov = tr->get(StringRef(versionKey));
	Future<uint64_t> ret = map(fov, [](Optional<FDBStandalone<StringRef>> ov) -> uint64_t {
		if (!ov.present())
			return 0;
		else
			return *((uint64_t*)(ov.get().begin()));
	});
	return ret;
}

std::string describeIndex(std::vector<std::pair<std::string, int>> indexKeys) {
	std::string ret = "index: ";
	for (const auto& indexKey : indexKeys) {
		ret += format("{%s:%d}, ", indexKey.first.c_str(), indexKey.second);
	}
	ret.resize(ret.length() - 2);
	return ret;
}

IndexInfo::IndexStatus indexStatus(const bson::BSONObj& indexObj) {
	const char* statusField = indexObj.getStringField(DocLayerConstants::STATUS_FIELD);
	if (strcmp(statusField, DocLayerConstants::INDEX_STATUS_READY) == 0)
		return IndexInfo::IndexStatus::READY;
	else if (strcmp(statusField, DocLayerConstants::INDEX_STATUS_BUILDING) == 0)
		return IndexInfo::IndexStatus::BUILDING;
	else
		return IndexInfo::IndexStatus::INVALID;
}

IndexInfo MetadataManager::indexInfoFromObj(const bson::BSONObj& indexObj, Reference<UnboundCollectionContext> cx) {
	IndexInfo::IndexStatus status = indexStatus(indexObj);
	bson::BSONObj keyObj = indexObj.getObjectField(DocLayerConstants::KEY_FIELD);
	std::vector<std::pair<std::string, int>> indexKeys;
	indexKeys.reserve(keyObj.nFields());
	for (auto i = keyObj.begin(); i.more();) {
		auto e = i.next();
		indexKeys.emplace_back(e.fieldName(), (int)e.Number());
	}
	if (verboseLogging) {
		TraceEvent("BD_getAndAddIndexes").detail("AddingIndex", describeIndex(indexKeys));
	}
	if (verboseConsoleOutput) {
		fprintf(stderr, "%s\n\n", describeIndex(indexKeys).c_str());
	}
	if (status == IndexInfo::IndexStatus::BUILDING) {
		return IndexInfo(indexObj.getStringField(DocLayerConstants::NAME_FIELD), indexKeys, cx, status,
		                 UID::fromString(indexObj.getStringField(DocLayerConstants::BUILD_ID_FIELD)),
		                 indexObj.getBoolField(DocLayerConstants::UNIQUE_FIELD));
	} else {
		return IndexInfo(indexObj.getStringField(DocLayerConstants::NAME_FIELD), indexKeys, cx, status, Optional<UID>(),
		                 indexObj.getBoolField(DocLayerConstants::UNIQUE_FIELD));
	}
}

/**
 * Create required directories in directory layer for the new database. Caller should make sure database doesn't
 * exist already.
 *
 * NOTE: Its not safe to create directories in parallel in one transaction. This actor creates one after another.
 */
// ACTOR static Future<Void> createDatabaseContext(Reference<Transaction> tr,
//                                                std::string dbName,
//                                                Reference<DirectorySubspace> rootDir) {
//	Void _ = wait(success(rootDir->create(tr, {StringRef(dbName)})));
//	Void _ = wait(success(rootDir->create(tr, {StringRef(dbName), StringRef(DocLayerConstants::SYSTEM_INDEXES)})));
//	// There is no need for metadata directory for index namespace. Just creating it for now, to keep other bits of the
//	// code happy. Ideally, we keep indexes directly as Tuples in higher level keyspace and get-rid of all this special
//	// handling. But, thats for another day as that needs change of on-disk format.
//	Void _ = wait(success(rootDir->create(tr, {StringRef(dbName), StringRef(DocLayerConstants::SYSTEM_INDEXES),
//	                                           StringRef(DocLayerConstants::METADATA)})));
//	return Void();
//}

/**
 * Create required directories in directory layer for the new collection. Caller should make sure collection doesn't
 * exist already.
 *
 * NOTE: Its not safe to create directories in parallel in one transaction. This actor creates one after another.
 */
ACTOR static Future<Reference<UnboundCollectionContext>>
createNewCollectionContext(Reference<Transaction> tr, Namespace ns, Reference<DirectorySubspace> rootDir) {
	//	bool dbExists = wait(rootDir->exists(tr, {StringRef(ns.first)}));
	//	if (!dbExists)
	//		Void _ = wait(createDatabaseContext(tr, ns.first, rootDir));
	//
	//	ASSERT(ns.second != DocLayerConstants::SYSTEM_INDEXES)

	state Reference<DirectorySubspace> collDir =
	    wait(rootDir->createOrOpen(tr, {StringRef(ns.first), StringRef(ns.second)}));
	state Reference<DirectorySubspace> metaDir = wait(
	    rootDir->createOrOpen(tr, {StringRef(ns.first), StringRef(ns.second), StringRef(DocLayerConstants::METADATA)}));

	auto ucx = Reference<UnboundCollectionContext>(new UnboundCollectionContext(collDir, metaDir));

	// Bump metadata version, so we can start at version 1.
	ucx->bumpMetadataVersion(tr);

	return ucx;
}

ACTOR static Future<std::pair<Reference<UnboundCollectionContext>, uint64_t>> constructContext(
    Namespace ns,
    Reference<DocTransaction> tr,
    DocumentLayer* docLayer,
    bool includeIndex,
    bool createCollectionIfAbsent) {
	try {
		// The initial set of directory reads take place in a separate transaction with the same read version as `tr'.
		// This hopefully prevents us from accidentally RYWing a directory that `tr' itself created, and then adding it
		// to the cache, when there's a chance that `tr' won't commit.
		state Reference<FDB::Transaction> snapshotTr(new Transaction(docLayer->database));
		FDB::Version v = wait(tr->tr->getReadVersion());
		snapshotTr->setVersion(v);
		state Future<Reference<DirectorySubspace>> fcollectionDirectory =
		    docLayer->rootDirectory->open(snapshotTr, {StringRef(ns.first), StringRef(ns.second)});
		state Future<Reference<DirectorySubspace>> findexDirectory = docLayer->rootDirectory->open(
		    snapshotTr, {StringRef(ns.first), StringRef(DocLayerConstants::SYSTEM_INDEXES)});
		state Reference<DirectorySubspace> metadataDirectory = wait(docLayer->rootDirectory->open(
		    snapshotTr, {StringRef(ns.first), StringRef(ns.second), StringRef(DocLayerConstants::METADATA)}));

		state Future<uint64_t> fv = getMetadataVersion(tr->tr, metadataDirectory);
		state Reference<DirectorySubspace> collectionDirectory = wait(fcollectionDirectory);
		state Reference<DirectorySubspace> indexDirectory = wait(findexDirectory);
		state Reference<UnboundCollectionContext> cx =
		    Reference<UnboundCollectionContext>(new UnboundCollectionContext(collectionDirectory, metadataDirectory));

		// Only include existing indexes into the context when it's NOT building a new index.
		// When it's building a new index, it's unnecessary and inefficient to pass each recorded returned by a
		// TableScan through the existing indexes.
		if (includeIndex) {
			state Reference<UnboundCollectionContext> indexCx = Reference<UnboundCollectionContext>(
			    new UnboundCollectionContext(indexDirectory, Reference<DirectorySubspace>()));
			state Reference<Plan> indexesPlan = getIndexesForCollectionPlan(indexCx, ns);
			std::vector<bson::BSONObj> allIndexes = wait(getIndexesTransactionally(indexesPlan, tr));

			for (const auto& indexObj : allIndexes) {
				IndexInfo index = MetadataManager::indexInfoFromObj(indexObj, cx);
				if (index.status != IndexInfo::IndexStatus::INVALID) {
					cx->addIndex(index);
				}
			}
		}

		uint64_t version = wait(fv);
		return std::make_pair(cx, version);
	} catch (Error& e) {
		if (e.code() != error_code_directory_does_not_exist && e.code() != error_code_parent_directory_does_not_exist)
			throw;
		// In this case, one or more of the directories didn't exist, so this is "implicit collection creation", so
		// there are no indexes and no version.

		bool rootExists = wait(docLayer->rootDirectory->exists(tr->tr));
		if (!rootExists)
			throw doclayer_metadata_changed();

		if (!createCollectionIfAbsent)
			throw collection_not_found();

		Reference<UnboundCollectionContext> ucx = wait(createNewCollectionContext(tr->tr, ns, docLayer->rootDirectory));

		return std::make_pair(ucx, -1); // So we don't pollute the cache in case this transaction never commits
	}
}

ACTOR static Future<Reference<UnboundCollectionContext>> assembleCollectionContext(Reference<DocTransaction> tr,
                                                                                   Namespace ns,
                                                                                   Reference<MetadataManager> self,
                                                                                   bool includeIndex,
                                                                                   bool createCollectionIfAbsent) {
	if (self->contexts.size() > 100)
		self->contexts.clear();

	auto match = self->contexts.find(ns);

	if (match == self->contexts.end()) {
		std::pair<Reference<UnboundCollectionContext>, uint64_t> unboundPair =
		    wait(constructContext(ns, tr, self->docLayer, includeIndex, createCollectionIfAbsent));

		// Here and below don't pollute the cache if we just created the directory, since this transaction might
		// not commit.
		if (unboundPair.second != -1) {
			auto insert_result = self->contexts.insert(std::make_pair(ns, unboundPair));
			// Somebody else may have done the lookup and finished ahead of us. Either way, replace it with ours (can no
			// longer optimize this by only replacing if ours is newer, because the directory may have moved or
			// vanished.
			if (!insert_result.second) {
				insert_result.first->second = unboundPair;
			}
		}
		return unboundPair.first;
	} else {
		state uint64_t oldVersion = (*match).second.second;
		state Reference<UnboundCollectionContext> oldUnbound = (*match).second.first;
		uint64_t version = wait(getMetadataVersion(tr->tr, oldUnbound->metadataDirectory));
		if (version != oldVersion) {
			std::pair<Reference<UnboundCollectionContext>, uint64_t> unboundPair =
			    wait(constructContext(ns, tr, self->docLayer, includeIndex, createCollectionIfAbsent));
			if (unboundPair.second != -1) {
				// Create the iterator again instead of making the previous value state, because the map could have
				// changed during the previous wait. Either way, replace it with ours (can no longer optimize this by
				// only replacing if ours is newer, because the directory may have moved or vanished.
				// std::map<std::pair<std::string, std::string>, std::pair<Reference<UnboundCollectionContext>,
				// uint64_t>>::iterator match = self->contexts.find(ns);
				auto match = self->contexts.find(ns);

				if (match != self->contexts.end())
					match->second = unboundPair;
				else
					self->contexts.insert(std::make_pair(ns, unboundPair));
			}
			return unboundPair.first;
		} else {
			return oldUnbound;
		}
	}
}

Future<Reference<UnboundCollectionContext>> MetadataManager::getUnboundCollectionContext(
    Reference<DocTransaction> tr,
    Namespace const& ns,
    bool allowSystemNamespace,
    bool includeIndex,
    bool createCollectionIfAbsent) {
	if (!allowSystemNamespace && startsWith(ns.second.c_str(), "system."))
		throw write_system_namespace();
	return assembleCollectionContext(tr, ns, Reference<MetadataManager>::addRef(this), includeIndex,
	                                 createCollectionIfAbsent);
}

Future<Optional<Reference<UnboundCollectionContext>>> MetadataManager::getUnboundCollectionContextV1(
	Reference<DocTransaction> tr,
	Namespace const& ns,
	bool allowSystemNamespace,
	bool includeIndex) {
	try {
		Reference<UnboundCollectionContext> mcx =
		    wait(getUnboundCollectionContext(tr, ns, allowSystemNamespace, includeIndex, false));
		return Optional<Reference<UnboundCollectionContext>>(mcx);
	} catch (Error &e) {
		return Optional<Reference<UnboundCollectionContext>>();
	}
}

Future<Reference<UnboundCollectionContext>> MetadataManager::refreshUnboundCollectionContext(
    Reference<UnboundCollectionContext> cx,
    Reference<DocTransaction> tr) {
	return assembleCollectionContext(tr, std::make_pair(cx->databaseName(), cx->collectionName()),
	                                 Reference<MetadataManager>::addRef(this), false, false);
}

ACTOR static Future<Void> buildIndex_impl(bson::BSONObj indexObj,
                                          Namespace ns,
                                          Standalone<StringRef> encodedIndexId,
                                          Reference<ExtConnection> ec,
                                          UID build_id) {
	state IndexInfo info;
	try {
		state Reference<DocTransaction> tr = ec->getOperationTransaction();
		state Reference<UnboundCollectionContext> mcx = wait(ec->mm->getUnboundCollectionContext(tr, ns, false, false));
		info = MetadataManager::indexInfoFromObj(indexObj, mcx);
		info.status = IndexInfo::IndexStatus::BUILDING;
		info.buildId = build_id;
		mcx->addIndex(info);

		state Reference<Plan> buildingPlan = ec->wrapOperationPlan(
		    ref(new BuildIndexPlan(ref(new TableScanPlan(mcx)), info, ns.first, encodedIndexId, ec->mm)), false, mcx);
		int64_t _ = wait(executeUntilCompletionTransactionally(buildingPlan, tr));

		state Reference<Plan> finalizePlan = ec->isolatedWrapOperationPlan(
		    ref(new UpdateIndexStatusPlan(ns, encodedIndexId, ec->mm,
		                                  std::string(DocLayerConstants::INDEX_STATUS_READY), build_id)),
		    0, -1);
		int64_t _ = wait(executeUntilCompletionTransactionally(finalizePlan, ec->getOperationTransaction()));

		return Void();
	} catch (Error& e) {
		TraceEvent(SevError, "indexRebuildFailed").error(e);
		state Error err = e;
		// try forever to set the index into an error status (unless somebody comes along before us and starts a
		// different build)
		loop {
			state bool okay;
			// Providing the build id here is sufficient to avoid clobbering a "ready" index as well, since
			// UpdateIndexStatusPlan, if it has that optional parameter, will return an error in the event that the
			// buildId field does not exist (as is the case for 'ready' indexes).
			state Reference<Plan> errorPlan = ec->isolatedWrapOperationPlan(
			    ref(new UpdateIndexStatusPlan(ns, encodedIndexId, ec->mm,
			                                  std::string(DocLayerConstants::INDEX_STATUS_ERROR), build_id)),
			    0, -1);
			try {
				int64_t _ = wait(executeUntilCompletionTransactionally(errorPlan, ec->getOperationTransaction()));
				okay = true;
			} catch (Error& e) {
				if (e.code() == error_code_index_wrong_build_id)
					throw e;
				okay = false;
				// Otherwise, we hit some other non-retryable problem trying to set the index metadata to an error
				// status (perhaps commit_unknown_result). Go around the loop again.
			}
			if (okay)
				throw err;
		}
	}
}

Future<Void> MetadataManager::buildIndex(bson::BSONObj indexObj,
                                         Namespace const& ns,
                                         Standalone<StringRef> encodedIndexId,
                                         Reference<ExtConnection> ec,
                                         UID build_id) {
	return buildIndex_impl(indexObj, ns, encodedIndexId, ec, build_id);
}
