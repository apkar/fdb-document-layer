/*
 * MetadataManager.h
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

#ifndef _METADATA_MANAGER_H_
#define _METADATA_MANAGER_H_

#pragma once

#include "Constants.h"
#include "QLContext.h"
#include "QLTypes.h"

#include "bindings/flow/DirectorySubspace.h"

using Namespace = std::pair<std::string, std::string>;

std::string fullCollNameToString(Namespace const& ns);

struct MetadataManager : ReferenceCounted<MetadataManager>, NonCopyable {
	explicit MetadataManager(struct DocumentLayer* docLayer) : docLayer(docLayer) {}
	~MetadataManager() = default;

	Future<Reference<UnboundCollectionContext>> getUnboundCollectionContext(Reference<DocTransaction> tr,
	                                                                        Namespace const& ns,
	                                                                        bool allowSystemNamespace = false);
	Future<Reference<UnboundCollectionContext>> refreshUnboundCollectionContext(Reference<UnboundCollectionContext> cx,
	                                                                            Reference<DocTransaction> tr);

	Future<Reference<UnboundCollectionContext>> indexesCollection(Reference<DocTransaction> tr,
	                                                              std::string const& dbName) {
		return getUnboundCollectionContext(tr, std::make_pair(dbName, std::string(DocLayerConstants::SYSTEM_INDEXES)),
		                                   true);
	}

	/**
	 * Checks the existence of the collection and creates if doesn't exist. This runs in its own transaction. Implicit
	 * collection creation can only happen on write operations. We will call this before all write operations. Cost of
	 * having a separate transaction is fine for collection creation, as it wouldn't happen often. But, the cost of GRV
	 * just for the check is not great. That would get better in future cleanups.
	 */
	Future<Void> createCollectionIfAbsent(Namespace ns);
	Future<bool> collectionExists(Namespace ns, Reference<Transaction> tr);
	Future<bool> collectionExists(Namespace ns);

	static Future<Void> buildIndex(bson::BSONObj indexObj,
	                               Namespace const& ns,
	                               Standalone<StringRef> encodedIndexId,
	                               Reference<struct ExtConnection> ec,
	                               UID build_id);
	static Reference<IndexInfo> indexInfoFromObj(const bson::BSONObj& indexObj, Reference<UnboundCollectionContext> cx);

	std::map<Namespace, Reference<UnboundCollectionContext>> metadataCache;
	DocumentLayer* docLayer;
};

#endif /* _METADATA_MANAGER_H_ */
