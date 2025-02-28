version 1.0

import "not_enough_outputs.wdl" as child


workflow wf {
    input {
    }

    call child.wf as childcall {
    input:
    }

    Int should_never_output = childcall.only_result - 1

    output {
        Int only_result = childcall.only_result * 2
    }
}

