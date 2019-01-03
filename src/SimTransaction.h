//
// Created by Bhaskar Muppana on 2019-01-02.
//

#ifndef FDB_DOC_LAYER_SIMTRANSACTION_H
#define FDB_DOC_LAYER_SIMTRANSACTION_H

#include "flow/flow.h"
#include "bindings/flow/FDBLoanerTypes.h"

using namespace FDB;

class SimTransaction : public ReferenceCounted<SimTransaction>, private NonCopyable, public FastAllocated<SimTransaction> {
 public:
	Future<Version> getReadVersion();

	Version v;
};

#endif //FDB_DOC_LAYER_SIMTRANSACTION_H
