cwlVersion: v1.2
class: Workflow

inputs:
  - id: arrayS
    type: string[]
    default: ['hello','world']

steps:
  hello:
    run:
      class: CommandLineTool
      inputs:
        s: string
      baseCommand: [echo]
      arguments: [ $(inputs.s)]
      outputs:
        out: stdout
        err: stderr
    in:
      - id: s
        source: arrayS
    scatter:
     - s
    out:
      - id: out
      - id: err

  list:
    run:
      class: CommandLineTool
      inputs:
       file: File
      baseCommand: [ ls , -lh]
      arguments: [ $(inputs.file) ]
      stdout: "list.out"
      stderr: "list.err"
      outputs: 
        outList: stdout
        errList: stderr
    in:
      - id: file
        linkMerge: merge_flattened
        source:
          - hello/out
          - hello/err
    scatter:
      - file
    out: 
      - id: outList
      - id: errList

outputs:
  - id: outList
    type: File[]
    outputSource: ["list/outList"]

  - id: errList
    type: File[]
    outputSource: ["list/errList"]


requirements:
  - class: ScatterFeatureRequirement
  - class: MultipleInputFeatureRequirement
